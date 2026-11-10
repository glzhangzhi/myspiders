"""模块注册表与装配（不碰 DB、不起调度器）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_modules_endpoint_lists_nav_from_registry():
    from app.main import app

    r = TestClient(app).get("/api/modules")
    assert r.status_code == 200
    mods = {m["id"]: m for m in r.json()["modules"]}
    for must in ("system", "recipe", "qimen", "nintendo", "finance"):
        assert must in mods, must
    nav_ids = [n["id"] for m in mods.values() for n in m["nav"]]
    assert len(nav_ids) == len(set(nav_ids)), "导航 id 重复"


def test_job_ids_are_unique_and_jobs_are_callable():
    from app.core.registry import modules

    ids = []
    for m in modules():
        for job in (m.jobs() if m.jobs else ()):
            assert callable(job.func), job.spider_id
            ids.append(job.spider_id)
    assert len(ids) == len(set(ids)), ids


def test_large_json_is_gzipped():
    from app.main import app

    r = TestClient(app).get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_gzip_middleware_leaves_sse_alone():
    """SSE 被压缩会被缓冲到攒够一块才发，实时状态推送就不实时了。"""
    from starlette.middleware import gzip as g

    assert "text/event-stream" in g.DEFAULT_EXCLUDED_CONTENT_TYPES


def test_missing_feature_module_is_reported_not_fatal(tmp_path, monkeypatch):
    """一个功能包导入失败：非严格模式下跳过并带 error，其余照常。"""
    import app.features as pkg
    from app.core import registry

    bad = tmp_path / "zz_broken"
    bad.mkdir()
    (bad / "__init__.py").write_text("")
    (bad / "module.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.setattr(pkg, "__path__", [*pkg.__path__, str(tmp_path)])
    monkeypatch.delenv("MYSPIDERS_STRICT_MODULES", raising=False)
    found = registry.discover()
    broken = [x for x in found if x.id == "zz_broken"]
    assert broken and broken[0].module is None and "boom" in broken[0].error
    assert any(x.id == "system" and x.module for x in found)


def test_media_ranges_are_never_gzipped(tmp_path):
    """`Range: bytes=0-` 的 206 被 gzip 后 Content-Range 与正文对不上，手机上拖不动进度条。"""
    from fastapi import FastAPI
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import FileResponse

    from app.main import app as _  # 导入即扩充排除清单

    assert _ is not None

    f = tmp_path / "a.mp3"
    f.write_bytes(b"\0" * 50_000)
    mini = FastAPI()
    mini.add_middleware(GZipMiddleware, minimum_size=1024)
    mini.get("/a")(lambda: FileResponse(f, media_type="audio/mpeg"))
    c = TestClient(mini)
    r = c.get("/a", headers={"Range": "bytes=0-", "Accept-Encoding": "gzip"})
    assert r.status_code == 206 and "content-encoding" not in r.headers
    assert r.headers["content-range"] == "bytes 0-49999/50000"
    r = c.get("/a", headers={"Range": "bytes=100-199", "Accept-Encoding": "gzip"})
    assert r.status_code == 206 and len(r.content) == 100
