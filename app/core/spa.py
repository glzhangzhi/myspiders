"""新前端（web/ 构建产物 app/dashboard/dist）的静态服务。

  * `/dashboard/assets/*`：文件名带内容哈希 → 一年 immutable 缓存，二次打开零请求。
  * `/dashboard/theme.css`：配色的单一事实来源，旧页面也 <link> 它 → no-cache。
  * `/dashboard` 与 `/dashboard/<任意路由>`：回 index.html（no-cache），由前端路由接管。
    旧面板的整页 URL（/dashboard/av 等）因此天然变成新外壳里的同名页面，书签不失效。

dist 不进 git：没构建过时 /dashboard 返回 503 并提示怎么构建，而不是一个让人
以为服务挂了的 404。
"""

from __future__ import annotations


from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from fastapi import Request

from app.core.pages import IMMUTABLE as _IMMUTABLE, NO_CACHE, html_response, theme_file
from app.core.paths import DASHBOARD_DIR

DIST = DASHBOARD_DIR / "dist"
IMMUTABLE = _IMMUTABLE
_TYPES = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
          ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json"}

router = APIRouter(tags=["Pages"])


@router.get("/dashboard/theme.css", include_in_schema=False)
def theme_css(request: Request):
    """带 ?v=<哈希> 的请求长期缓存（地址随内容变）；不带版本的老引用仍 no-cache。"""
    built = DIST / "theme.css"
    p = built if built.exists() else theme_file()
    if not p.exists():
        raise HTTPException(status_code=404, detail="theme.css 未找到")
    headers = IMMUTABLE if request.query_params.get("v") else NO_CACHE
    return FileResponse(p, media_type="text/css", headers=headers)


@router.get("/dashboard/assets/{name:path}", include_in_schema=False)
def asset(name: str):
    base = (DIST / "assets").resolve()
    p = (base / name).resolve()
    if base not in p.parents or not p.is_file():   # 防路径穿越
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type=_TYPES.get(p.suffix, "application/octet-stream"), headers=IMMUTABLE)


def _index():
    index = DIST / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h1>前端尚未构建</h1><p>在仓库根目录执行 <code>pnpm -C web install && pnpm -C web build</code>"
            "（或 <code>scripts/deploy.sh --web-only</code>）。</p>",
            status_code=503,
        )
    return html_response(index)


@router.get("/dashboard", include_in_schema=False)
def dashboard():
    return _index()


@router.get("/dashboard/{path:path}", include_in_schema=False)
def dashboard_route(path: str):
    if path.startswith("assets/") or path == "theme.css":
        raise HTTPException(status_code=404)
    return _index()
