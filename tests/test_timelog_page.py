"""计时 PWA 原型（第 1 步）的页面与 manifest 守门。

不进 lifespan（不起调度器、不连 Mongo）：只验证路由、manifest 形状与两条静态约定。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

DASH = Path(__file__).resolve().parents[1] / "app" / "dashboard"


@pytest.fixture(scope="module")
def client():
    # 不用 with：with 会触发 lifespan
    return TestClient(app)


def test_page_served_no_cache(client):
    r = client.get("/timelog")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"


def test_manifest_shape(client):
    r = client.get("/timelog/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.content)
    assert m["scope"] == "/timelog"
    assert m["start_url"].startswith(m["scope"])
    assert m["display"] == "standalone"
    # 只放固定项：shortcuts 在安装时写死，「最常用的几个」放进来也不会跟着变（docs/timelog.md）
    assert len(m["shortcuts"]) == 3
    for s in m["shortcuts"]:
        assert s["url"].startswith("/timelog?action=")
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_shortcut_actions_handled_by_page():
    """manifest 里每个 action 页面都得认，否则长按桌面图标点了没反应、也不报错。"""
    m = json.loads((DASH / "timelog.webmanifest").read_text(encoding="utf-8"))
    html = (DASH / "timelog.html").read_text(encoding="utf-8")
    for s in m["shortcuts"]:
        action = s["url"].split("action=", 1)[1]
        assert f"a === '{action}'" in html, action


@pytest.mark.parametrize("size", [192, 512])
def test_icons_are_png(client, size):
    r = client.get(f"/timelog/icon-{size}.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("size", ["64", "../timelog"])
def test_icon_size_whitelisted(client, size):
    assert client.get(f"/timelog/icon-{size}.png").status_code in (404, 422)


def test_manifest_link_sends_credentials():
    """panel. 在 Cloudflare Access 后面：不带 cookie 取 manifest 只会拿到登录页。"""
    html = (DASH / "timelog.html").read_text(encoding="utf-8")
    assert re.search(r'<link rel="manifest"[^>]*crossorigin="use-credentials"', html)


def test_page_follows_theme():
    html = (DASH / "timelog.html").read_text(encoding="utf-8")
    assert '/dashboard/theme.css' in html
    # 配色走 theme.css 变量，写死的 hex 不跟主题走、漏改不报错
    assert not re.findall(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b", html)


def test_requests_never_follow_redirects():
    """Access 会话过期时请求被 302 到 cloudflareaccess.com。跟随的话是跨域无 CORS，
    fetch 抛的 TypeError 与断网一模一样 —— 会被当成离线永远重试，而该做的是重新登录。"""
    core = (DASH / "timelog-core.js").read_text(encoding="utf-8")
    assert core.count("fetch(") == 1 and "redirect: 'manual'" in core
    html = (DASH / "timelog.html").read_text(encoding="utf-8")
    assert "fetch(" not in html          # 页面只经 TL.api / TL.flush 发请求


def test_service_worker_scope_header(client):
    """sw.js 在 /timelog/ 下，默认作用域不含页面本身 /timelog，要靠这个头放宽。"""
    r = client.get("/timelog/sw.js")
    assert r.status_code == 200
    assert r.headers["service-worker-allowed"] == "/timelog"
    assert client.get("/timelog/core.js").status_code == 200
    html = (DASH / "timelog.html").read_text(encoding="utf-8")
    assert "register('/timelog/sw.js', { scope: '/timelog' })" in html


def test_url_actions_in_reminders_are_handled():
    """提醒通知上的按钮跳 ?action=…，页面不认就是点了没反应、也不报错。"""
    svc = (DASH.parent / "features" / "timelog" / "service.py").read_text(encoding="utf-8")
    html = (DASH / "timelog.html").read_text(encoding="utf-8")
    actions = set(re.findall(r"\?action=([\w-]+)", svc))
    assert actions >= {"pause", "mute-today"}
    for a in actions:
        assert f"a === '{a}'" in html, a
