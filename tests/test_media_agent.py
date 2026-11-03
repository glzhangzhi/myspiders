"""3060ti 上跑的 media agent 的纯函数。

agent 在 `deploy/win-media/`（不是 app 包），用 importlib 按路径加载。
fixtures 里的 ltaoo 响应取自其 OpenAPI 文档的示例（docs/public/openapi/），
真机校准后有出入就以真机为准更新 fixture。

    .venv/bin/pytest tests/test_media_agent.py -v
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT = ROOT / "deploy" / "win-media" / "media_agent.py"
FIX = ROOT / "tests" / "fixtures" / "media"


def _load():
    spec = importlib.util.spec_from_file_location("media_agent_under_test", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load()


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ── 只用标准库 ───────────────────────────────────────────────────────────

def test_agent_imports_only_stdlib():
    """它跑在只有系统 python + yt-dlp venv 的 WSL 里。多一个三方依赖 =
    那边多一步部署，忘了装就是一次「agent 意外退出」的静默故障。"""
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    std = set(sys.stdlib_module_names) | {"__future__"}
    assert mods <= std, f"非标准库依赖：{sorted(mods - std)}"


# ── 文件名 ───────────────────────────────────────────────────────────────

def test_win_safe_name_replaces_illegal_chars():
    s = A.win_safe_name('a/b\\c:d*e?f"g<h>i|j')
    assert not any(c in s for c in '/\\:*?"<>|')
    assert s.startswith("a")


def test_win_safe_name_strips_trailing_dot_and_space():
    """Windows 会静默吃掉结尾的点和空格：建出来的目录名和程序以为的不一样。"""
    assert A.win_safe_name("abc. . ") == "abc"


def test_win_safe_name_reserved_and_empty():
    assert A.win_safe_name("CON") == "_CON"
    assert A.win_safe_name("nul.txt") == "_nul.txt"
    assert A.win_safe_name("   ") == "untitled"
    assert A.win_safe_name("\n\t") == "untitled"


def test_win_safe_name_truncates_by_chars_not_bytes():
    s = A.win_safe_name("中" * 200, max_len=80)
    assert s == "中" * 80


def test_win_safe_name_collapses_whitespace():
    assert A.win_safe_name("a\n\nb\tc") == "a b c"


# ── yt-dlp 进度 ──────────────────────────────────────────────────────────

def test_progress_line_parsed():
    p = A.parse_ytdlp_progress("MEDIAPROG|5000|10000|NA|250000.5|12")
    assert p == {"downloaded": 5000, "total": 10000, "pct": 50.0, "speed": 250000, "eta": 12}


def test_progress_falls_back_to_estimate():
    p = A.parse_ytdlp_progress("MEDIAPROG|2500|NA|10000|NA|NA")
    assert p["pct"] == 25.0 and p["total"] == 10000 and p["speed"] is None and p["eta"] is None


def test_progress_unknown_total():
    p = A.parse_ytdlp_progress("MEDIAPROG|2500|NA|NA|NA|NA")
    assert p["pct"] is None and p["downloaded"] == 2500


def test_non_progress_lines_ignored():
    assert A.parse_ytdlp_progress("[download] Destination: x.mp4") is None
    assert A.parse_ytdlp_progress("") is None


def test_progress_template_matches_parser():
    """模板和解析器是一对，改了一个忘了另一个 → 进度条永远是 0。"""
    tpl = A.ytdlp_progress_template()
    assert tpl.startswith("download:MEDIAPROG|")
    assert tpl.count("|") == 5


# ── ltaoo 响应 ───────────────────────────────────────────────────────────

def test_feed_list_parsed():
    ok, inner, err = A.ltaoo_payload(fx("ltaoo_feed_list_success.json"))
    assert ok and err is None
    items, last_buffer, has_more, n_raw = A.parse_feed_list(inner)
    assert n_raw == 1 and len(items) == 1
    it = items[0]
    assert it["key"] == "14545102784038246591"
    assert it["nonce_id"]
    assert it["author"] == "我不叫螃蟹."
    assert it["author_id"].endswith("@finder")
    assert it["published_at"] == 1733911369
    assert it["duration"] == 7
    assert has_more is False  # 不满 15 条


def test_feed_list_skips_non_video():
    inner = {"object": [
        {"id": "1", "objectDesc": {"media": [{"mediaType": 2}]}},       # 图文
        {"id": "2", "liveInfo": {"streamUrl": "x"}, "objectDesc": {}},  # 直播
        {"id": "3", "objectDesc": {"mediaType": 9, "media": []}},       # 直播无 media
        {"id": "4", "objectDesc": {"description": "v", "media": [{"mediaType": 4}]}, "contact": {}},
    ], "lastBuffer": "abc"}
    items, lb, has_more, n_raw = A.parse_feed_list(inner)
    assert [i["key"] for i in items] == ["4"]
    assert lb == "abc" and n_raw == 4 and has_more is False


def test_feed_list_has_more_when_full_page():
    obj = {"id": "1", "objectDesc": {"media": [{"mediaType": 4}]}}
    _, _, has_more, _ = A.parse_feed_list({"object": [dict(obj, id=str(i)) for i in range(15)],
                                           "lastBuffer": "x"})
    assert has_more is True


def test_ltaoo_error_envelopes():
    assert A.ltaoo_payload({"code": 400, "msg": "视频号页面未连接"}) == (False, None, "视频号页面未连接")
    ok, _, err = A.ltaoo_payload({"code": 0, "data": {"errCode": -1, "errMsg": "busy"}})
    assert not ok and err == "busy"
    ok, inner, _ = A.ltaoo_payload(fx("ltaoo_task_list_single.json"))
    assert ok and inner["id"] == 44


def test_contacts_parsed():
    ok, inner, _ = A.ltaoo_payload(fx("ltaoo_contact_search_success.json"))
    cs = A.parse_contacts(inner)
    assert [c["nickname"] for c in cs] == ["我不叫螃蟹.", "辣个螃蟹.海鲜店"]
    assert all(c["username"].endswith("@finder") for c in cs)


def test_task_status():
    _, task, _ = A.ltaoo_payload(fx("ltaoo_task_list_single.json"))
    st = A.parse_task_status(task)
    assert st["status"] == 0 and not st["finished"]
    assert st["path"].endswith("example.json")
    done = A.parse_task_status({"status": 5, "size": 100, "downloaded": 100,
                                "files": [{"kind": "video", "file_path": "D:\\x.mp4"}]})
    assert done["finished"] and done["ok"] and done["path"] == "D:\\x.mp4" and done["pct"] == 100.0
    failed = A.parse_task_status({"status": 6, "error": "403"})
    assert failed["finished"] and not failed["ok"] and failed["error"] == "403"


def test_task_create_response_shape():
    """create 的返回里任务 id 在 tasks[0].data.id；部分失败时 tasks[0].code != 0。"""
    ok, data, _ = A.ltaoo_payload(fx("ltaoo_task_create_success.json"))
    assert ok and data["tasks"][0]["data"]["id"] == 42
    ok, data, _ = A.ltaoo_payload(fx("ltaoo_task_create_partial_failure.json"))
    assert ok and data["tasks"][0]["code"] == 400


# ── 翻页判据 ─────────────────────────────────────────────────────────────

def test_paging_does_not_stop_on_pinned_old_video():
    """作者置顶的老视频排在第一页最前面。「遇到已知就停」会让新作品永远检测不到。"""
    known = {"old_pinned"}
    page = ["old_pinned", "new1", "new2"] + [f"n{i}" for i in range(12)]
    assert A.should_continue_paging(page, known) is True


def test_paging_stops_when_page_mostly_known():
    known = {f"k{i}" for i in range(15)}
    assert A.should_continue_paging([f"k{i}" for i in range(15)], known) is False


def test_yt_auth_error_detection():
    assert A.yt_auth_error("ERROR: [youtube:tab] PLx: YouTube said: The playlist does not exist.")
    assert A.yt_auth_error("Sign in to confirm you're not a bot")
    assert not A.yt_auth_error("ERROR: unable to download video data: HTTP Error 403")


def test_to_windows_path():
    assert A.to_windows_path("/mnt/d/Downloads/YouTube/a b/x.mp4") == "D:\\Downloads\\YouTube\\a b\\x.mp4"
    assert A.to_windows_path("/mnt/c") == "C:\\"
    assert A.to_windows_path("/root/x") == "/root/x"
    assert A.to_windows_path("D:\\x.mp4") == "D:\\x.mp4"


# ── 真机样本（2026-11-01，3060ti 上 ltaoo v260907）───────────────────────

def test_real_feed_list_page():
    ok, inner, _ = A.ltaoo_payload(fx("real_feed_list_page.json"))
    items, lb, has_more, n_raw = A.parse_feed_list(inner)
    assert ok and n_raw == 1 and len(items) == 1
    assert items[0]["key"] and items[0]["published_at"]
    assert has_more is True, "真实接口带 continueFlag，应该以它为准而不是数条数"


def test_real_search_failure_is_an_error():
    """微信搜索实测一直回 -70003 JSAPI_JSONPARSE_FAILED。必须被当成错误
    （好让 agent 退回到关注列表），而不是当成「搜到 0 个」。"""
    ok, _, err = A.ltaoo_payload(fx("real_search_failed.json"))
    assert not ok and err == "JSAPI_JSONPARSE_FAILED"


def test_follow_list_and_local_match():
    ok, inner, _ = A.ltaoo_payload(fx("real_follow_list.json"))
    cs, lb, more = A.parse_follow_list(inner)
    assert ok and len(cs) == 3 and lb and more is True
    assert [c["nickname"] for c in A.match_contacts("烤肉小王", cs)] == ["烤肉小王"]
    assert [c["nickname"] for c in A.match_contacts("肉", cs)] == ["烤肉小王", "肉肉先生"]
    assert len(A.match_contacts("", cs)) == 3
    assert A.match_contacts("不存在", cs) == []


def test_real_task_record_mime_kind():
    """真机 files[].kind 是 "video/mp4"，第一版只认 "video" 导致完成后路径为空。"""
    st = A.parse_task_status({"status": 5, "size": 10, "downloaded": 10, "progress": 100, "files": [
        {"kind": "video/mp4", "file_path": "D:\\Downloads\\视频号\\x\\a_.mp4"}]})
    assert st["ok"] and st["path"].endswith("a_.mp4") and st["pct"] == 100.0


def test_progress_from_bytes_not_progress_field():
    st = A.parse_task_status({"status": 2, "size": 2000, "downloaded": 1000, "progress": 0.1})
    assert st["pct"] == 50.0


def test_wx_filename():
    obj = {"id": "123", "createtime": 1793543924,
           "objectDesc": {"description": "今天去爬山了 #户外 #登山\n好累"}}
    name = A.wx_filename(obj)
    assert name.startswith("2026") and "今天去爬山了" in name and "#" not in name and "\n" not in name
    assert A.wx_filename({"id": "123", "objectDesc": {"description": "#只有话题"}}) == "123"


def test_yt_cookie_hint():
    rotated = ("WARNING: [youtube:tab] The provided YouTube account cookies are no longer valid. "
               "They have likely been rotated in the browser as a security measure.\n"
               "ERROR: [youtube:tab] PLx: YouTube said: The playlist does not exist.")
    assert "已失效" in A.yt_cookie_hint(rotated) and "Firefox" in A.yt_cookie_hint(rotated)
    assert "登录" in A.yt_cookie_hint("ERROR: The playlist does not exist.")
    assert A.yt_cookie_hint("ERROR: HTTP Error 403") == ""


# ── 补全历史（2026-11-02：74 个作者全卡在首次检测的 15 条）─────────────────

class _FakeCtx:
    def __init__(self, pages):
        self.cfg = dict(A.DEFAULT_CONFIG)
        self.pages = pages
        self.calls = 0

    def check(self):
        pass


def _run_scan(monkeypatch, src, pages):
    """pages: 每页的 feed id 列表；最后一页 continueFlag=0。"""
    ctx = _FakeCtx(pages)
    events = []
    monkeypatch.setattr(A, "emit", lambda ev, **kw: events.append((ev, kw)))
    monkeypatch.setattr(A, "_sleep", lambda c, s: None)

    def fake_call(c, path, params):
        i = int(params["next_marker"] or 0)
        c.calls += 1
        objs = [{"id": k, "objectDesc": {"media": [{"mediaType": 4}]}} for k in pages[i]]
        last = i == len(pages) - 1
        return {"object": objs, "lastBuffer": "" if last else str(i + 1), "continueFlag": 0 if last else 1}

    monkeypatch.setattr(A, "wx_call", fake_call)
    A.scan_wx_source(ctx, src)
    found = [kw for ev, kw in events if ev == "found"][0]
    return ctx, found


PAGES = [[f"p{p}_{i}" for i in range(15)] for p in range(5)]


def test_backfill_pages_past_known(monkeypatch):
    """已知的正是第一页；「全部 + 未补全」必须越过它们翻到底。"""
    src = {"id": "wx:a", "key": "a", "mode": "all", "baseline_done": True, "full_done": False,
           "known_ids": PAGES[0]}
    ctx, found = _run_scan(monkeypatch, src, PAGES)
    assert ctx.calls == 5 and found["exhausted"] is True
    assert len(found["items"]) == 60


def test_incremental_stops_on_known(monkeypatch):
    src = {"id": "wx:a", "key": "a", "mode": "all", "baseline_done": True, "full_done": True,
           "known_ids": PAGES[0] + PAGES[1]}
    ctx, found = _run_scan(monkeypatch, src, PAGES)
    assert ctx.calls == 1 and found["items"] == []


def test_old_server_without_full_done_keeps_incremental(monkeypatch):
    """agent 先于服务端上线时 payload 没有 full_done：不能被当成「要补全」狂翻。"""
    src = {"id": "wx:a", "key": "a", "mode": "all", "baseline_done": True, "known_ids": PAGES[0]}
    ctx, _ = _run_scan(monkeypatch, src, PAGES)
    assert ctx.calls == 1


def test_new_only_first_scan_one_page(monkeypatch):
    src = {"id": "wx:a", "key": "a", "mode": "new_only", "baseline_done": False, "known_ids": []}
    ctx, found = _run_scan(monkeypatch, src, PAGES)
    assert ctx.calls == 1 and len(found["items"]) == 15 and found["exhausted"] is False


def test_age_gate_error_is_short_and_explained():
    err = ("ERROR: [youtube] L-mMv5GURn0: Sign in to confirm your age. This video may be inappropriate for "
           "some users. Use --cookies-from-browser or --cookies for the authentication. See  "
           "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies.")
    line = A.last_error_line(err)
    assert "Use --cookies" not in line and "https://" not in line and "confirm your age" in line
    assert "年龄限制" in A.yt_cookie_hint(err)


def test_cookie_state_reflects_real_outcome(tmp_path, monkeypatch):
    """面板原先只看 cookie 文件里「有没有登录字段」，失效了照样显示正常。"""
    monkeypatch.setattr(A, "COOKIE_STATE", str(tmp_path / "s.json"))
    A.note_cookie_result("WARNING: The provided YouTube account cookies are no longer valid.", True)
    assert A.read_cookie_state()["ok"] is False
    A.note_cookie_result("", True)
    assert A.read_cookie_state()["ok"] is True
    A.note_cookie_result("WARNING: no longer valid", False)   # 没用 cookie 的运行不算数
    assert A.read_cookie_state()["ok"] is True


def test_cookie_file_copy_is_fresh_each_time(tmp_path, monkeypatch):
    """副本不能持久保留：失效时 yt-dlp 会把删掉登录字段的结果写回，副本就永久残了。"""
    src = tmp_path / "cookies.txt"
    src.write_text("# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tv1\n")
    monkeypatch.setattr(A, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(A, "FIREFOX_GLOB", str(tmp_path / "nope" / "*.sqlite"))
    ctx = A.Ctx({"yt_cookies": str(src)})
    args = A.yt_cookie_args(ctx)
    assert args[0] == "--cookies"
    pathlib.Path(args[1]).write_text("# 被 yt-dlp 写残了\n")
    args2 = A.yt_cookie_args(ctx)
    assert "SID" in pathlib.Path(args2[1]).read_text()


def test_firefox_profile_preferred(tmp_path, monkeypatch):
    import sqlite3
    prof = tmp_path / "Profiles" / "abc.default"
    prof.mkdir(parents=True)
    con = sqlite3.connect(prof / "cookies.sqlite")
    con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
    con.execute("INSERT INTO moz_cookies VALUES ('.youtube.com', 'LOGIN_INFO', 'x')")
    con.commit(); con.close()
    monkeypatch.setattr(A, "FIREFOX_GLOB", str(tmp_path / "Profiles" / "*" / "cookies.sqlite"))
    monkeypatch.setattr(A, "TMP_DIR", str(tmp_path / "tmp"))
    ctx = A.Ctx({"yt_cookies": str(tmp_path / "none.txt")})
    assert A.yt_cookie_args(ctx) == ["--cookies-from-browser", "firefox:" + str(prof)]
