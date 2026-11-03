"""媒体下载：检测 / 下载 / 停止的编排。

## 一次只跑一件事

全局**非阻塞**锁：检测和下载共用。那台电脑上 agent 也有自己的 pidfile 锁，
两层是因为服务器这层要能立刻回「已有任务在跑」（而不是 ssh 过去才知道），
那边那层防的是服务器重启后旧 ssh 会话还没断干净时的重入。

## 断了怎么办

- ssh 中途断（关机、睡眠、断网）：在跑的条目退回 pending、本次运行记 failed，
  **不推送** —— 恢复手段就是「下次开机再点一下」，推了也只是噪音。
- myspider 自己重启：启动时 `recover_on_startup()` 把 downloading 打回 pending、
  running 的运行记为 interrupted。远端 agent 由断管 / 父进程消失自己收尾。

## 推送

只有两种（topic `media`，都是 low：路过看见就行，动作是「开电脑点按钮」不急）：
- **自动**检测发现了新条目 —— 手动点的检测结果就在眼前，不推；
- 一批下载结束的汇总。
离线、没有新东西、手动检测，一律不推（「没消息」不推通知，见 push_service）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.media.repo import get_media_repo
from app.features.media import agent_client as agent
from app.features.media import rules as R

logger = get_logger("media")

_lock = threading.Lock()
_stop_flag = threading.Event()
_current: dict[str, Any] = {"run_id": None, "kind": None, "agent": None}


class Busy(Exception):
    pass


class Offline(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_busy() -> bool:
    return _lock.locked()


# ─────────────────────────────────────────────────────────────────────────
# 来源
# ─────────────────────────────────────────────────────────────────────────

def add_source(platform: str, raw: str, pick: Optional[dict] = None) -> dict:
    """添加来源。视频号如果给了 pick（前端搜索后点选的候选），直接按 username 建。"""
    repo = get_media_repo()
    if platform == "wx" and pick and pick.get("username"):
        parsed = R.ParsedSource("wx", pick["username"], pick["username"])
        return repo.add_source(parsed, name=pick.get("nickname") or "",
                               extra={"head_url": pick.get("head_url") or ""})
    parsed = R.parse_source_input(platform, raw)
    if parsed.error:
        raise ValueError(parsed.error)
    return repo.add_source(parsed)


def pick_candidate(sid: str, username: str) -> dict:
    repo = get_media_repo()
    src = repo.get_source(sid)
    if not src:
        raise ValueError("来源不存在")
    cand = next((c for c in src.get("candidates") or [] if c.get("username") == username), None)
    return repo.rekey_source(sid, username, (cand or {}).get("nickname") or src.get("name", ""),
                             (cand or {}).get("head_url") or "")


def search_wx(keyword: str) -> dict:
    agent.push_agent()
    evs = agent.run_once("search_wx", {"keyword": keyword}, timeout=120)
    ev = next((e for e in evs if e.get("ev") == "search"), {})
    end = next((e for e in evs if e.get("ev") == "end"), {})
    return {"candidates": ev.get("candidates") or [], "note": ev.get("note") or "",
            "error": ev.get("error") or end.get("error")}


# ─────────────────────────────────────────────────────────────────────────
# 运行
# ─────────────────────────────────────────────────────────────────────────

class _RunLog:
    """把事件里的日志攒起来，最多每 2 秒落一次库（下载进度事件很密）。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.buf: list[str] = []
        self.current: Optional[dict] = None
        self.dirty = False
        self.last = 0.0

    def line(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.buf.append(f"{stamp} {msg}")
        self.dirty = True

    def set_current(self, cur: Optional[dict]) -> None:
        self.current = cur
        self.dirty = True

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self.dirty or (not force and now - self.last < 2):
            return
        get_media_repo().update_run(self.run_id, {"current": self.current}, self.buf)
        self.buf = []
        self.dirty = False
        self.last = now


def _start(kind: str, trigger: str) -> str:
    if not _lock.acquire(blocking=False):
        raise Busy("已有任务在跑")
    _stop_flag.clear()
    try:
        run_id = get_media_repo().start_run(kind, trigger)
    except Exception:
        _lock.release()
        raise
    _current.update(run_id=run_id, kind=kind, agent=None)
    return run_id


def _end() -> None:
    _current.update(run_id=None, kind=None, agent=None)
    agent.invalidate_ping()
    _lock.release()


def scan(trigger: str = "manual", source_ids: Optional[list[str]] = None) -> dict:
    """检测一次。同步执行（调用方放线程里跑）。"""
    repo = get_media_repo()
    sources = [s for s in repo.list_sources()
               if s.get("status") != "paused" and (not source_ids or s["_id"] in source_ids)]
    if not sources:
        return {"status": "ok", "new": 0, "note": "没有来源"}

    st = agent.ping(force=True)
    if not st.get("online"):
        if trigger == "auto":
            logger.info("媒体检测：3060ti 离线，跳过")
        raise Offline("3060ti 离线")

    run_id = _start("scan", trigger)
    rl = _RunLog(run_id)
    counts = {"sources": len(sources), "new": 0, "errors": 0}
    new_by_platform = {"wx": 0, "yt": 0}
    by_id = {s["_id"]: s for s in sources}
    status, error = "ok", None
    try:
        payload_sources = [{
            "id": s["_id"], "platform": s["platform"], "key": s.get("key") or "",
            "input": s.get("input") or "", "name": s.get("name") or "",
            "mode": s.get("mode"), "baseline_done": bool(s.get("baseline_done")),
            # 缺字段的老来源按「已补全」算，只有显式切到「全部」时才会被置 False
            "full_done": bool(s.get("full_done", True)),
            "known_ids": repo.known_keys(s["_id"]) if s["platform"] == "wx" else [],
        } for s in sources]
        run = agent.AgentRun("scan", {"sources": payload_sources}, idle_timeout=900)
        _current["agent"] = run
        rl.line(f"开始检测 {len(sources)} 个来源")
        for ev in run.events(should_stop=_stop_flag.is_set):
            kind = ev.get("ev")
            sid = ev.get("source_id")
            if kind == "log":
                rl.line(ev.get("msg", ""))
            elif kind == "wx_status" and not ev.get("available"):
                rl.line("⚠ 视频号页面未连接（电脑微信里打开视频号）")
            elif kind == "resolved" and sid in by_id:
                new = repo.rekey_source(sid, ev["username"], ev.get("nickname", ""), ev.get("head_url", ""))
                by_id[new["_id"]] = new
                by_id[sid] = new
                rl.line(f"已绑定作者 {ev.get('nickname')}")
            elif kind == "needs_pick" and sid in by_id:
                repo.patch_source(sid, {"status": "needs_pick", "candidates": ev.get("candidates") or [],
                                        "last_scan_at": _now(),
                                        "last_scan_error": "昵称匹配到多个或零个作者，请在面板里选一个"})
                rl.line(f"「{by_id[sid].get('input')}」需要手动选作者")
            elif kind == "found" and sid in by_id:
                src = by_id[sid]
                if ev.get("name") and ev["name"] != src.get("name"):
                    repo.patch_source(src["_id"], {"name": ev["name"]})
                    src = {**src, "name": ev["name"]}
                item_status = R.initial_status(src.get("mode") or "new_only", bool(src.get("baseline_done")))
                n = repo.upsert_found(src, ev.get("items") or [], item_status)
                n_pending = n if item_status == R.PENDING else 0
                fields = {"last_scan_at": _now(), "last_scan_error": None,
                          "last_new_count": n_pending, "baseline_done": True, "status": "active"}
                if src.get("mode") == "all" and src.get("full_done") is False and ev.get("exhausted"):
                    fields["full_done"] = True
                    rl.line(f"{src.get('name') or src.get('input')}：历史作品已补全（共翻 {ev.get('pages')} 页）")
                repo.patch_source(src["_id"], fields)
                counts["new"] += n_pending
                new_by_platform[src["platform"]] += n_pending
                label = src.get("name") or src.get("input")
                if item_status == R.SKIPPED and n:
                    rl.line(f"{label}：首次检测，{n} 个历史作品记为「已跳过」（可手动加回队列）")
                else:
                    rl.line(f"{label}：新增 {n_pending}")
            elif kind == "source_error" and sid in by_id:
                counts["errors"] += 1
                src = by_id[sid]
                fields = {"last_scan_error": ev.get("error")}
                if not ev.get("transient"):
                    fields["last_scan_at"] = _now()
                repo.patch_source(src["_id"], fields)
                rl.line(f"✗ {src.get('name') or src.get('input')}：{ev.get('error')}")
            elif kind == "end":
                status = ev.get("status") or "ok"
                error = ev.get("error")
            rl.flush()
        if _stop_flag.is_set():
            status = "stopped"
    except agent.AgentOffline:
        status, error = "failed", "3060ti 离线"
    except Exception as e:  # noqa: BLE001
        logger.exception("媒体检测失败")
        status, error = "failed", str(e)[:400]
    finally:
        rl.line(f"结束：{status}" + (f"（{error}）" if error else ""))
        rl.flush(force=True)
        repo.finish_run(run_id, status, counts, error)
        _end()

    if trigger == "auto" and counts["new"] > 0:
        parts = [f"{label} {new_by_platform[p]}" for p, label in (("wx", "视频号"), ("yt", "YouTube"))
                 if new_by_platform[p]]
        _push("有新内容待下载", "、".join(parts) + " 个新条目，开电脑后到面板点「开始下载」")
    return {"status": status, "error": error, **counts}


def download(ids: Optional[list[str]] = None, trigger: str = "manual") -> dict:
    repo = get_media_repo()
    items = repo.pending_items(ids)
    if not items:
        return {"status": "ok", "done": 0, "failed": 0, "note": "待下载列表是空的"}
    st = agent.ping(force=True)
    if not st.get("online"):
        raise Offline("3060ti 离线")
    free = st.get("free_gb")
    if free is not None and free < settings.media_min_free_gb:
        raise ValueError(f"D 盘只剩 {free} GB（下限 {settings.media_min_free_gb:.0f} GB），先腾点地方")

    run_id = _start("download", trigger)
    rl = _RunLog(run_id)
    counts = {"total": len(items), "done": 0, "failed": 0}
    by_id = {it["_id"]: it for it in items}
    started: set[str] = set()
    status, error = "ok", None
    try:
        repo.set_status(list(by_id), R.DOWNLOADING, only_from=[R.PENDING])
        payload = [{
            "id": it["_id"], "platform": it["platform"], "key": it["key"],
            "nonce_id": it.get("nonce_id") or "", "url": it.get("url") or "",
            "title": it.get("title") or "", "author": it.get("author") or "",
            "source_name": it.get("source_name") or "",
        } for it in items]
        run = agent.AgentRun("download", {"items": payload}, idle_timeout=1800)
        _current["agent"] = run
        rl.line(f"开始下载 {len(items)} 个")
        for ev in run.events(should_stop=_stop_flag.is_set):
            kind = ev.get("ev")
            iid = ev.get("id")
            it = by_id.get(iid) if iid else None
            if kind == "log":
                rl.line(ev.get("msg", ""))
            elif kind == "item_start" and it:
                started.add(iid)
                repo.set_item(iid, {"run_id": run_id, "started_at": _now()}, inc={"attempts": 1})
                rl.set_current({"id": iid, "title": it.get("title"), "platform": it["platform"], "pct": 0})
                rl.line(f"↓ {it.get('title', '')[:60]}")
            elif kind == "progress" and it:
                prog = {k: ev.get(k) for k in ("pct", "speed", "eta", "downloaded", "total")}
                rl.set_current({"id": iid, "title": it.get("title"), "platform": it["platform"], **prog})
            elif kind == "item_done" and it:
                counts["done"] += 1
                repo.set_item(iid, {"status": R.DONE, "done_at": _now(), "error": None,
                                    "file_path": ev.get("path") or "", "file_size": ev.get("size")})
                rl.line(f"✓ {it.get('title', '')[:60]}" + (f"（{ev['note']}）" if ev.get("note") else ""))
            elif kind == "item_failed" and it:
                counts["failed"] += 1
                repo.set_item(iid, {"status": R.FAILED, "error": ev.get("error")})
                rl.line(f"✗ {it.get('title', '')[:60]}：{ev.get('error')}")
            elif kind == "end":
                status = ev.get("status") or "ok"
                error = ev.get("error")
            rl.flush()
        if _stop_flag.is_set():
            status = "stopped"
    except agent.AgentOffline:
        status, error = "failed", "3060ti 离线"
    except Exception as e:  # noqa: BLE001
        logger.exception("媒体下载失败")
        status, error = "failed", str(e)[:400]
    finally:
        # 没跑完的（停止 / 断线 / 还没轮到）一律退回待下载
        n_back = repo.reset_downloading("上次下载被中断" if status != "ok" else None)
        if n_back:
            rl.line(f"{n_back} 个未完成的退回待下载")
        rl.line(f"结束：{status}，完成 {counts['done']}，失败 {counts['failed']}"
                + (f"（{error}）" if error else ""))
        rl.flush(force=True)
        repo.finish_run(run_id, status, counts, error)
        _end()

    if counts["done"] or counts["failed"]:
        body = f"完成 {counts['done']} 个"
        if counts["failed"]:
            body += f"，失败 {counts['failed']} 个（面板里可一键重试）"
        if status != "ok":
            body += "；本批" + {"stopped": "已手动停止", "failed": "中途出错"}.get(status, status)
        _push("下载结束", body)
    return {"status": status, "error": error, **counts}


def stop() -> dict:
    """停止当前任务：本地断 ssh + 远端 stop 双保险。"""
    _stop_flag.set()
    run = _current.get("agent")
    msgs = []
    try:
        evs = agent.stop_remote()
        msgs = [e.get("msg") for e in evs if e.get("ev") == "log"]
    except agent.AgentOffline:
        msgs = ["3060ti 离线"]
    except Exception as e:  # noqa: BLE001
        msgs = [f"远端停止失败：{e}"]
    if run is not None:
        run.kill()
    return {"ok": True, "busy": is_busy(), "messages": msgs}


def update_tools() -> dict:
    if not _lock.acquire(blocking=False):
        raise Busy("已有任务在跑")
    try:
        agent.push_agent()
        evs = agent.run_once("update_tools", timeout=700)
        end = next((e for e in evs if e.get("ev") == "end"), {})
        return {"status": end.get("status"), "messages": [e.get("msg") for e in evs if e.get("ev") == "log"]}
    finally:
        agent.invalidate_ping()
        _lock.release()


def run_in_background(fn, *args, **kwargs) -> None:
    """路由里用：先在调用线程抢锁失败就立刻报 Busy，抢到了再交给后台线程。"""
    if is_busy():
        raise Busy("已有任务在跑")

    def _target():
        try:
            fn(*args, **kwargs)
        except (Busy, Offline) as e:
            logger.info("媒体任务未执行：%s", e)
        except Exception:  # noqa: BLE001
            logger.exception("媒体后台任务异常")

    threading.Thread(target=_target, name=f"media-{fn.__name__}", daemon=True).start()


def recover_on_startup() -> None:
    repo = get_media_repo()
    n_runs = repo.interrupt_running("myspider 重启，运行被中断")
    n_items = repo.reset_downloading("myspider 重启，下载被中断")
    if n_runs or n_items:
        logger.info("媒体下载启动恢复：%d 个运行记为中断，%d 个条目退回待下载", n_runs, n_items)


async def scheduled_scan() -> None:
    """定时任务入口：离线/忙都静默跳过（离线是常态，不是故障）。"""
    import asyncio
    try:
        await asyncio.to_thread(scan, "auto")
    except Offline:
        return
    except Busy:
        logger.info("媒体检测：已有任务在跑，本轮跳过")


def status() -> dict:
    repo = get_media_repo()
    running = repo.running_run()
    return {
        "agent": agent.ping(),
        "busy": is_busy(),
        "run": _jsonable(running) if running else None,
        "counts": repo.count_by_status(),
        "scan_interval_minutes": settings.media_scan_interval_minutes,
    }


def _push(title: str, body: str) -> None:
    from app.core.push import PushServiceError, send_note
    try:
        # low：路过看见就行。动作是「开电脑点按钮」，不急，也不值得响
        send_note(title, body, topic=settings.media_ntfy_topic, priority="low",
                  tags=["arrow_down"], click_url=settings.public_url("/dashboard/media"))
    except PushServiceError as e:
        logger.warning("媒体推送失败：%s", e)


def _jsonable(doc: dict) -> dict:
    out = dict(doc)
    if "_id" in out:
        out["_id"] = str(out["_id"])
    return out
