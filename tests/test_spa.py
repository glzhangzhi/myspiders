"""新前端的静态服务：路由回退、缓存头、主题文件、旧页面挂载位置。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import spa
from app.main import app

c = TestClient(app)


def _fake_dist(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=app></div>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    monkeypatch.setattr(spa, "DIST", dist)
    return dist


def test_spa_routes_fall_back_to_index_with_no_cache(tmp_path, monkeypatch):
    _fake_dist(tmp_path, monkeypatch)
    for path in ("/dashboard", "/dashboard/nintendo", "/dashboard/av", "/dashboard/x/y"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "id=app" in r.text
        assert r.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable_and_traversal_is_blocked(tmp_path, monkeypatch):
    _fake_dist(tmp_path, monkeypatch)
    r = c.get("/dashboard/assets/index-abc123.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert c.get("/dashboard/assets/missing.js").status_code == 404
    assert c.get("/dashboard/assets/..%2F..%2Findex.html").status_code == 404


def test_unbuilt_frontend_says_how_to_build(tmp_path, monkeypatch):
    monkeypatch.setattr(spa, "DIST", tmp_path / "nope")
    r = c.get("/dashboard")
    assert r.status_code == 503 and "pnpm" in r.text


def test_theme_css_served_even_without_build(tmp_path, monkeypatch):
    monkeypatch.setattr(spa, "DIST", tmp_path / "nope")
    r = c.get("/dashboard/theme.css")
    assert r.status_code == 200 and "--accent" in r.text and r.headers["cache-control"] == "no-cache"


def test_legacy_pages_live_under_legacy_prefix():
    from app.core.registry import modules

    pages = [p for m in modules() for p in m.legacy_pages]
    assert pages and all(p.url.startswith("/legacy/") for p in pages)
    for p in pages:
        assert c.get(p.url).status_code == 200, p.url
    assert c.get("/legacy/common.js").status_code == 200
    navs = [n for m in modules() for n in m.nav if n.legacy_url]
    assert all(n.legacy_url in {p.url for p in pages} for n in navs)


def test_html_links_theme_css_with_content_hash():
    """主题文件地址带内容哈希：改了配色地址就变，浏览器不会拿着旧 theme.css 不放。"""
    import re

    from app.core.pages import theme_hash

    v = theme_hash()
    assert re.fullmatch(r"[0-9a-f]{10}", v)
    for url in ("/legacy/recipe", "/timelog"):
        html = c.get(url).text
        assert f'href="/dashboard/theme.css?v={v}"' in html, url
        assert 'href="/dashboard/theme.css"' not in html, url
    r = c.get(f"/dashboard/theme.css?v={v}")
    assert "immutable" in r.headers["cache-control"]
    assert c.get("/dashboard/theme.css").headers["cache-control"] == "no-cache"
