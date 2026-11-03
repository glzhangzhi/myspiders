"""把各模块声明的旧版整页 HTML（LegacyPage）挂成路由。

页面一律 no-cache：这些 HTML 走 FileResponse、改完免重启就该立刻生效。
不给这个头时浏览器会对带 Last-Modified 的响应启用**启发式缓存**
（约取"距上次修改"的 10% 作为新鲜期），期间连条件请求都不发 —— 表现为
改了文件刷新却还是旧页面。内嵌页更坑：iframe 的子框架连硬刷新都未必重取。
no-cache 不是不缓存，是"每次必须回源校验"，命中 304 时只传头、不传正文。
"""

from __future__ import annotations

from typing import Iterable

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.core.module import LegacyPage
from app.core.paths import DASHBOARD_DIR

NO_CACHE = {"Cache-Control": "no-cache"}
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

# ── theme.css 的缓存击穿 ──
# 新外壳、旧页面、计时 PWA 都 <link> 同一个 /dashboard/theme.css。地址固定不变时，
# 浏览器内存缓存或中间的 CDN 留着旧版就会出现「点了切换主题、按钮文字变了、颜色不变」
# —— 2026-11-03 实测：新版加了浅色配色，机主浏览器里还是没有浅色的旧文件，强制刷新才好。
# 所以返回 HTML 时把地址改写成带内容哈希的 /dashboard/theme.css?v=<hash>：
# 配色一改地址就变，浏览器必然重取；带哈希的版本可以放心长期缓存。
_THEME_HREF = re.compile(r'href="/dashboard/theme\.css(?:\?[^"]*)?"')
_theme_cache: dict[str, object] = {"key": None, "hash": ""}


def theme_file() -> Path:
    from app.core.paths import REPO_ROOT

    built = DASHBOARD_DIR / "dist" / "theme.css"
    return built if built.exists() else REPO_ROOT / "web" / "public" / "theme.css"


def theme_hash() -> str:
    p = theme_file()
    try:
        st = p.stat()
    except OSError:
        return ""
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _theme_cache["key"] != key:
        _theme_cache.update(key=key, hash=hashlib.sha1(p.read_bytes()).hexdigest()[:10])
    return str(_theme_cache["hash"])


def html_response(path: Path) -> HTMLResponse:
    """读 HTML、把 theme.css 换成带版本的地址，no-cache 返回。"""
    text = path.read_text(encoding="utf-8")
    v = theme_hash()
    if v:
        text = _THEME_HREF.sub(f'href="/dashboard/theme.css?v={v}"', text)
    return HTMLResponse(text, headers=NO_CACHE)


def legacy_router(pages: Iterable[LegacyPage]) -> APIRouter:
    router = APIRouter(tags=["Pages"])

    @router.get("/legacy/common.js", include_in_schema=False)
    def _common_js():
        """旧页面共享的前端工具脚本。"""
        return FileResponse(DASHBOARD_DIR / "common.js", media_type="application/javascript", headers=NO_CACHE)
    for page in pages:
        def _serve(_page: LegacyPage = page):
            if _page.file.exists():
                return html_response(_page.file)
            raise HTTPException(status_code=404, detail=f"{_page.summary or _page.url} 未找到")

        router.add_api_route(page.url, _serve, methods=["GET"], summary=page.summary or page.url,
                             include_in_schema=False)
    return router
