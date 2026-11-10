"""MySpiders — 个人多源采集与管理面板。

本文件只做装配：功能全部在 `app/features/<id>/module.py` 里声明（路由、定时任务、
生命周期钩子、导航），由 `app.core.registry` 自动发现。这里不认识任何具体功能 ——
公开仓库里缺几个私有功能目录时，这个文件一行不用改。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware import gzip as _gzip_mod
from starlette.responses import RedirectResponse

from app.core.logging import get_logger
from app.core.pages import legacy_router
from app.core.registry import loaded, modules
from app.core.scheduler import spider_scheduler
from app.core.spa import router as spa_router
from app.core.settings import settings

logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = get_logger("MAIN")


async def _run_hook(hook, label: str) -> None:
    """同步或 async 钩子都能挂；单个钩子出错只记日志，不拖垮启动/关闭。"""
    if hook is None:
        return
    try:
        result = hook()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        logger.exception("模块钩子 %s 执行失败", label)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("application start（%d 个功能模块）", len(modules()))

    # on_load 在注册任务之前：Mercor 会用库里的配置覆盖 settings 出厂默认值，
    # 任务说明与任务本身都读这些字段。
    for m in modules():
        if m.on_load:
            await asyncio.to_thread(m.on_load)

    if not settings.background_tasks_enabled:
        logger.warning("BACKGROUND_TASKS_ENABLED=false：不启动调度器 / 长连接 / 启动钩子")
        yield
        logger.info("application shutdown")
        return

    for m in modules():
        for job in (m.jobs() if m.jobs else ()):
            spider_scheduler.register(
                spider_id=job.spider_id, name=job.name, func=job.func, trigger=job.trigger,
                description=job.description, category=job.category, schedule=job.schedule,
                enabled_default=job.enabled_default,
            )
    spider_scheduler.start()

    for m in modules():
        await _run_hook(m.on_startup, f"{m.id}.on_startup")

    yield

    for m in modules():
        await _run_hook(m.on_shutdown, f"{m.id}.on_shutdown")
    spider_scheduler.shutdown()
    logger.info("application shutdown")


app = FastAPI(title="MySpiders", lifespan=lifespan)

# HTML / JSON 一律 gzip。SSE 不受影响：starlette 的 GZipMiddleware 默认排除 text/event-stream。
# 音视频/图片绝不能压：`Range: bytes=0-` 的 206 被压缩后 Content-Range 还是原始偏移、正文却是
# gzip 流，浏览器拖进度条就乱了（starlette 0.46 实测会压），几百 MB 的文件还白烧 CPU。
# GZipResponder 在运行时读这个模块全局量，扩充它即可（tests/test_registry.py 端到端守着）。
_gzip_mod.DEFAULT_EXCLUDED_CONTENT_TYPES = tuple(dict.fromkeys(
    (*_gzip_mod.DEFAULT_EXCLUDED_CONTENT_TYPES, "audio/", "video/", "image/")))
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/", include_in_schema=False)
def _root_to_dashboard():
    """根路径 → 仪表盘（书签写根地址即可）。"""
    return RedirectResponse(url="/dashboard")


@app.get("/api/health", tags=["system"])
def health_check():
    return {"status": "ok"}


@app.get("/api/modules", tags=["system"], summary="已加载的功能模块与导航条目")
def list_modules():
    """导航的唯一事实来源：前端按这里的 nav 渲染侧边栏。

    导入失败的模块也列出来（带 error），否则少一个入口很难察觉。
    """
    out = []
    for item in loaded():
        m = item.module
        if m is None:
            out.append({"id": item.id, "error": item.error, "nav": []})
            continue
        out.append({
            "id": m.id,
            "title": m.title,
            "nav": [
                {"id": n.id, "title": n.title, "icon": n.icon, "group": n.group,
                 "order": n.order, "legacy_url": n.legacy_url}
                for n in m.nav
            ],
        })
    return {"modules": out}


for _m in modules():
    for _r in _m.routers:
        app.include_router(_r)
    for _exc, _handler in _m.exception_handlers.items():
        app.add_exception_handler(_exc, _handler)
app.include_router(legacy_router(p for _m in modules() for p in _m.legacy_pages))
# 最后挂：/dashboard/{path} 是兜底路由，必须排在所有具体路由之后
app.include_router(spa_router)
