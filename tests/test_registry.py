"""模块注册表与装配（不碰 DB、不起调度器）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_modules_endpoint_lists_nav_from_registry():
    from app.main import app

    r = TestClient(app).get("/api/modules")
    assert r.status_code == 200
    mods = {m["id"]: m for m in r.json()["modules"]}
    for must in ("system", "recipe", "qimen", "nintendo", "timelog"):
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
