"""把各模块声明的旧版整页 HTML（LegacyPage）挂成路由。

页面一律 no-cache：这些 HTML 走 FileResponse、改完免重启就该立刻生效。
不给这个头时浏览器会对带 Last-Modified 的响应启用**启发式缓存**
（约取"距上次修改"的 10% 作为新鲜期），期间连条件请求都不发 —— 表现为
改了文件刷新却还是旧页面。内嵌页更坑：iframe 的子框架连硬刷新都未必重取。
no-cache 不是不缓存，是"每次必须回源校验"，命中 304 时只传头、不传正文。
"""

from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.module import LegacyPage
from app.core.paths import DASHBOARD_DIR

NO_CACHE = {"Cache-Control": "no-cache"}


def legacy_router(pages: Iterable[LegacyPage]) -> APIRouter:
    router = APIRouter(tags=["Pages"])

    @router.get("/legacy/common.js", include_in_schema=False)
    def _common_js():
        """旧页面共享的前端工具脚本。"""
        return FileResponse(DASHBOARD_DIR / "common.js", media_type="application/javascript", headers=NO_CACHE)
    for page in pages:
        def _serve(_page: LegacyPage = page):
            if _page.file.exists():
                return FileResponse(_page.file, media_type="text/html", headers=NO_CACHE)
            raise HTTPException(status_code=404, detail=f"{_page.summary or _page.url} 未找到")

        router.add_api_route(page.url, _serve, methods=["GET"], summary=page.summary or page.url,
                             include_in_schema=False)
    return router
