"""计时 PWA（手机端记录页）的静态资源路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.core.paths import DASHBOARD_DIR
from app.core.pages import NO_CACHE as _NO_CACHE, html_response

router = APIRouter(tags=["Timelog"])

# ── 计时 PWA（手机端记录页）──
# 挂在 /timelog 而非 /dashboard/ 下：manifest 的 scope 就是这个前缀，独立前缀让
# 「装到桌面的应用」只覆盖这一页，点进面板其他链接会跳出 standalone 窗口而不是
# 把整个面板吞进来。设计与约定见 docs/timelog.md。

TIMELOG_ICONS = DASHBOARD_DIR / "timelog-icons"


@router.get("/timelog", summary="计时（手机端）", response_class=HTMLResponse)
async def timelog_page():
    html = DASHBOARD_DIR / "timelog.html"
    if html.exists():
        return html_response(html)
    raise HTTPException(status_code=404, detail="计时页面未找到")


@router.get("/timelog/manifest.webmanifest", include_in_schema=False)
async def timelog_manifest():
    path = DASHBOARD_DIR / "timelog.webmanifest"
    if path.exists():
        return FileResponse(path, media_type="application/manifest+json", headers=_NO_CACHE)
    raise HTTPException(status_code=404, detail="manifest 未找到")


@router.get("/timelog/core.js", include_in_schema=False)
async def timelog_core_js():
    """存储与同步（页面与 service worker 共用）。"""
    path = DASHBOARD_DIR / "timelog-core.js"
    if path.exists():
        return FileResponse(path, media_type="application/javascript", headers=_NO_CACHE)
    raise HTTPException(status_code=404, detail="core.js 未找到")


@router.get("/timelog/sw.js", include_in_schema=False)
async def timelog_service_worker():
    # 脚本在 /timelog/ 下，默认作用域是 "/timelog/" —— 偏偏不含页面本身 "/timelog"。
    # 放宽到 "/timelog" 需要这个响应头，页面注册时再显式传 scope。
    path = DASHBOARD_DIR / "timelog-sw.js"
    if path.exists():
        return FileResponse(path, media_type="application/javascript",
                            headers={**_NO_CACHE, "Service-Worker-Allowed": "/timelog"})
    raise HTTPException(status_code=404, detail="sw 未找到")


@router.get("/timelog/icon-{size}.png", include_in_schema=False)
async def timelog_icon(size: int):
    # 白名单：size 来自 URL，不能直接拼进路径
    if size not in (192, 512):
        raise HTTPException(status_code=404, detail="图标不存在")
    path = TIMELOG_ICONS / f"icon-{size}.png"
    if path.exists():
        return FileResponse(path, media_type="image/png", headers=_NO_CACHE)
    raise HTTPException(status_code=404, detail="图标不存在")
