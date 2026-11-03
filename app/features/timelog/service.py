"""计时（timelog）的业务层：同步、网页编辑、类型与设置。

两个入口，同一条路：
  - **手机** `sync(ops)`：上传自己攒下的操作（ts 是手机点下去的时刻）；
  - **网页**各编辑接口：服务端代为生成操作（ts = 服务器此刻）再走同一个 `_apply`。
所以无论谁改的，记录都只由「重放操作日志」得到（规则见 timelog_replay）。

网页的编辑在写操作前**先按记录当前状态校验**，不合法直接 400：网页是交互式的，
能当场告诉人哪里错了。手机的操作则不做这种校验 —— 它们可能是离线攒的、乱序到的，
现在看不合理的，前面的操作到齐后可能就合理了，交给重放去判（不合理的进 conflicts）。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.core.settings import settings as app_settings
from app.features.timelog.repo import DEFAULT_SETTINGS, N_COLORS, get_timelog_repo, now_ms
from app.features.timelog import remind as remind
from app.features.timelog import replay as R

log = logging.getLogger(__name__)

MAX_BATCH = 500

# 插入操作与重放物化必须串行：两批同时改同一条记录时，后写的物化视图可能
# 基于「另一批插入之前」读到的操作，把对方的效果覆盖掉。单用户、单进程，一把锁够了。
_lock = threading.Lock()


class TimelogError(ValueError):
    """给人看的业务错误（路由转成 400/404）。"""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _apply(ops: list[dict], source: str) -> tuple[list[str], list[str]]:
    repo = get_timelog_repo()
    with _lock:
        inserted, dup = repo.insert_ops(ops, source)
        repo.rematerialize({o["rid"] for o in ops if o["id"] in set(inserted)})
    return inserted, dup


def _new_op(kind: str, rid: str, data: Optional[dict] = None, ts: Optional[int] = None) -> dict:
    op = {"id": str(uuid.uuid4()), "ts": ts if ts is not None else now_ms(), "kind": kind, "rid": rid}
    if data:
        op["data"] = data
    err = R.validate_op(op, now_ms())
    if err:
        raise TimelogError(err)
    return op


# ═══════════════════════════ 手机：同步 ═══════════════════════════

def sync(ops: list[Any]) -> dict:
    """接收手机攒下的一批操作。

    返回三类 id，**客户端据此把它们全部移出同步队列**：
      - accepted：新入库；
      - duplicate：早已入库（上次的响应丢了而重发）—— 与 accepted 同等对待；
      - rejected：形状不合法，重发也不会成功，客户端应丢弃（并留一行日志）。
    再附上最新 state，手机用它覆盖本地视图。
    """
    if len(ops) > MAX_BATCH:
        raise TimelogError(f"一次最多同步 {MAX_BATCH} 条，请分批")
    now = now_ms()
    good, rejected, seen = [], [], set()
    for op in ops:
        oid = op.get("id") if isinstance(op, dict) else None
        reason = R.validate_op(op, now) if isinstance(op, dict) else "操作必须是对象"
        if reason is None and oid in seen:
            continue                             # 同一批里重复：只算一次
        if reason:
            rejected.append({"id": oid, "reason": reason})
        else:
            seen.add(oid)
            good.append({k: op.get(k) for k in ("id", "ts", "kind", "rid", "data")})
    inserted, dup = _apply(good, "phone")
    return {"accepted": inserted, "duplicate": dup, "rejected": rejected, "state": state()}


def state() -> dict:
    """手机启动/同步后要的全部东西：进行中的记录、按钮（带参考时长）、类型、设置。"""
    repo = get_timelog_repo()
    now = now_ms()
    durations = repo.button_durations_ms()
    buttons = []
    for b in repo.list_buttons():
        b["reference"] = R.reference([ms / 60_000 for ms in durations.get(b["id"], [])])
        buttons.append(b)
    active = [_with_derived(r, now) for r in repo.active_records()]
    return {
        "server_now": now,
        "settings": repo.get_settings(),
        "types": repo.list_types(),
        "buttons": buttons,
        "active": sorted(active, key=lambda r: -r["updated_ts"]),
        # 设置页里告诉人「手机上的 ntfy 要订阅哪个」—— 没订阅等于没提醒，且不报错
        "notify_topic": app_settings.ntfy_topic_prefix + app_settings.timelog_ntfy_topic,
    }


def _with_derived(rec: dict, now: int) -> dict:
    rec = dict(rec)
    rec.update(R.derived(rec, now))
    return rec


# ═══════════════════════════ 网页：查看 ═══════════════════════════

def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """本地某一天的 [00:00, 次日 00:00)，转成 UTC。夏令时切换日是 23 或 25 小时。"""
    z = ZoneInfo(tz)
    start = datetime.combine(day, time(0), tzinfo=z)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=z)
    return start, end


def day(day_str: str) -> dict:
    try:
        d = date.fromisoformat(day_str)
    except ValueError:
        raise TimelogError("日期格式应为 YYYY-MM-DD")
    repo = get_timelog_repo()
    tz = repo.get_settings()["timezone"]
    start, end = local_day_bounds(d, tz)
    now = now_ms()
    return {
        "date": d.isoformat(), "timezone": tz,
        "start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000),
        "records": [_with_derived(r, now) for r in repo.records_between(start, end)],
    }


def unnamed() -> list[dict]:
    return get_timelog_repo().unnamed_records()


def get_record(rid: str, with_ops: bool = False) -> dict:
    repo = get_timelog_repo()
    rec = repo.get_record(rid)
    if rec is None:
        raise TimelogError("记录不存在", 404)
    rec = _with_derived(rec, now_ms())
    if with_ops:
        rec["ops"] = sorted(repo.ops_for(rid), key=R.sort_key)
    return rec


# ═══════════════════════════ 网页：编辑 ═══════════════════════════

def _live(rid: str) -> dict:
    rec = get_timelog_repo().get_record(rid)
    if rec is None or rec["status"] in R.TOMBSTONES:
        raise TimelogError("记录不存在", 404)
    return rec


def _check_type(type_id: Optional[str]) -> None:
    if type_id is not None and get_timelog_repo().get_type(type_id) is None:
        raise TimelogError("类型不存在")


def create_record(fields: dict, segments: list[dict]) -> dict:
    """补记（需求 3.5/3.6）：在时间线上选一段空白，直接生成一条已完成的记录。

    允许与已有记录重叠（2026-11 机主定）。
    """
    _check_type(fields.get("type_id"))
    try:
        segs = R.validate_segments(segments, open_tail=False, now_ms=now_ms())
    except R.SegmentError as e:
        raise TimelogError(str(e))
    data = {k: v for k, v in fields.items() if v is not None}
    if "estimate" in data:
        data["estimate_source"] = "late"       # 事后补的预估不是预估，不进偏差统计
    data["segments"] = segs
    rid = str(uuid.uuid4())
    _apply([_new_op("create", rid, data)], "web")
    return get_record(rid)


def edit_record(rid: str, fields: dict) -> dict:
    """改名称/类型/备注/预估。网页改预估一律标 late（见 timelog_replay.ESTIMATE_SOURCES）。"""
    _live(rid)
    if "type_id" in fields:
        _check_type(fields["type_id"])
    data = dict(fields)
    if "name" in data and isinstance(data["name"], str):
        data["name"] = data["name"].strip() or None
    if "estimate" in data:
        data["estimate_source"] = "late" if data["estimate"] is not None else None
    if not data:
        raise TimelogError("没有要改的字段")
    _apply([_new_op("edit", rid, data)], "web")
    return get_record(rid)


def set_segments(rid: str, segments: list[dict]) -> dict:
    """改开始/结束时间及各时间段（需求 3.11）。计时中的记录最后一段保持开放。"""
    rec = _live(rid)
    try:
        segs = R.validate_segments(segments, open_tail=rec["status"] == "running", now_ms=now_ms())
    except R.SegmentError as e:
        raise TimelogError(str(e))
    _apply([_new_op("set_segments", rid, {"segments": segs})], "web")
    return get_record(rid)


def delete_record(rid: str) -> None:
    _live(rid)
    _apply([_new_op("delete", rid)], "web")


def split_record(rid: str, at_ms: int) -> list[dict]:
    """在 at 处把一条已完成的记录切成两条（需求 3.11）。

    前半保留原 id 与全部字段；后半是新记录，复制名称/类型/备注，但**不带按钮和预估**：
    预估是对整件事的，拆开后对任何一半都不成立；按钮历史若两半都算，那个按钮的
    参考时长就会被一次拆分拉低一半。
    """
    rec = _live(rid)
    if rec["status"] != "done":
        raise TimelogError("只有已结束的记录才能拆分")
    try:
        left, right = R.split_segments(rec["segments"], at_ms)
    except R.SegmentError as e:
        raise TimelogError(str(e))
    new_rid = str(uuid.uuid4())
    ts = now_ms()
    data = {k: rec.get(k) for k in ("name", "type_id", "note") if rec.get(k) is not None}
    data["segments"] = right
    _apply([
        _new_op("set_segments", rid, {"segments": left}, ts),
        _new_op("create", new_rid, data, ts),
    ], "web")
    return [get_record(rid), get_record(new_rid)]


def merge_records(rids: list[str]) -> dict:
    """把几条已完成的记录并成一条（需求 3.11）。

    保留最早开始的那条（id、按钮、预估都随它）；时间段取并集（重叠只算一次）；
    名称/类型缺的从其余记录里补；备注按时间顺序拼起来；其余记录删除。
    """
    if len(set(rids)) < 2:
        raise TimelogError("至少选两条记录")
    recs = [_live(r) for r in dict.fromkeys(rids)]
    if any(r["status"] != "done" for r in recs):
        raise TimelogError("只有已结束的记录才能合并")
    recs.sort(key=lambda r: r["segments"][0]["s"])
    keep, rest = recs[0], recs[1:]
    edit: dict[str, Any] = {}
    for k in ("name", "type_id"):
        if keep.get(k) is None:
            v = next((r[k] for r in rest if r.get(k) is not None), None)
            if v is not None:
                edit[k] = v
    notes = [r["note"] for r in recs if r.get("note")]
    if len(notes) > 1 or (notes and not keep.get("note")):
        edit["note"] = "\n\n".join(notes)
    ts = now_ms()
    segs = R.coalesce([g for r in recs for g in r["segments"]])
    ops = [_new_op("set_segments", keep["id"], {"segments": segs}, ts)]
    if edit:
        ops.append(_new_op("edit", keep["id"], edit, ts))
    ops += [_new_op("delete", r["id"], None, ts) for r in rest]
    _apply(ops, "web")
    return get_record(keep["id"])


# ═══════════════════════════ 类型 ═══════════════════════════

def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise TimelogError("名称不能为空，且不超过 50 字")
    return name.strip()


def _check_color(color: Any) -> Optional[int]:
    if color is None:
        return None
    if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color < N_COLORS:
        raise TimelogError(f"color 是 0–{N_COLORS - 1} 的色号")
    return color


def list_types(include_archived: bool = False) -> list[dict]:
    return get_timelog_repo().list_types(include_archived)


def create_type(name: Any, color: Any = None) -> dict:
    repo = get_timelog_repo()
    name = _clean_name(name)
    if any(t["name"] == name for t in repo.list_types(include_archived=True)):
        raise TimelogError("已有同名类型")
    tid = repo.create_type(name, _check_color(color))
    return next(t for t in repo.list_types(True) if t["id"] == tid)


def update_type(tid: str, fields: dict) -> dict:
    """改名、换色、排序、归档/恢复。**没有真删除**：历史记录还指着它（需求 3.3 的「删除」= 归档）。"""
    repo = get_timelog_repo()
    out: dict[str, Any] = {}
    if "name" in fields:
        out["name"] = _clean_name(fields["name"])
        if any(t["name"] == out["name"] and t["id"] != tid for t in repo.list_types(True)):
            raise TimelogError("已有同名类型")
    if "color" in fields:
        out["color"] = _check_color(fields["color"])
    if "order" in fields:
        if isinstance(fields["order"], bool) or not isinstance(fields["order"], (int, float)):
            raise TimelogError("order 必须是数字")
        out["order"] = fields["order"]
    if "archived" in fields:
        out["archived"] = bool(fields["archived"])
    if not out:
        raise TimelogError("没有要改的字段")
    if not repo.update_type(tid, out):
        raise TimelogError("类型不存在", 404)
    return next(t for t in repo.list_types(True) if t["id"] == tid)


# ═══════════════════════════ 设置 ═══════════════════════════

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def get_settings() -> dict:
    return get_timelog_repo().get_settings()


def update_settings(fields: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in DEFAULT_SETTINGS:
            raise TimelogError(f"不认识的设置：{k}")
        if k == "switch_behavior" and v not in ("pause", "end"):
            raise TimelogError("switch_behavior 只能是 pause 或 end")
        if k in ("idle_remind_minutes", "overtime_cap_minutes") and (
                isinstance(v, bool) or not isinstance(v, int) or not 5 <= v <= 24 * 60):
            raise TimelogError(f"{k} 是 5–1440 的整数（分钟）")
        if k == "overtime_ratio" and (
                isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 10):
            raise TimelogError("overtime_ratio 是 1–10 的倍数")
        if k in ("quiet_start", "quiet_end") and not (isinstance(v, str) and _HHMM.match(v)):
            raise TimelogError(f"{k} 格式是 HH:MM")
        if k == "quiet_weekends" and not isinstance(v, bool):
            raise TimelogError("quiet_weekends 是布尔值")
        if k == "timezone":
            try:
                ZoneInfo(v)
            except Exception:
                raise TimelogError("不认识的时区")
        out[k] = v
    if not out:
        raise TimelogError("没有要改的设置")
    return get_timelog_repo().update_settings(out)


# ═══════════════════════════ 提醒 ═══════════════════════════

def mute_today() -> dict:
    """「今天不再提醒」：到本地次日 0 点前什么都不推（需求 3.12）。"""
    repo = get_timelog_repo()
    until = remind.end_of_local_day(now_ms(), repo.get_settings()["timezone"])
    repo.set_muted_until(until)
    return {"muted_until": until}


def check_reminders() -> int:
    """定时任务：判断此刻该不该推空闲 / 超时提醒，返回推了几条。

    去重键落 Mongo（不是进程内变量）：本服务改代码就重启，进程内去重等于每次
    重启都把刚推过的再推一遍。推失败不记键，下一轮再试。
    """
    from app.core import push as push_service

    repo = get_timelog_repo()
    now = now_ms()
    cfg = {**repo.get_settings(), **repo.remind_state()}
    active = [_with_derived(r, now) for r in repo.active_records()]
    items = remind.evaluate(now, cfg, active, repo.last_phone_ts())
    sent_keys = {x["key"] for x in cfg["sent"]}
    todo = [i for i in items if i["key"] not in sent_keys]
    if not todo:
        log.debug("timelog 提醒：无需推送（候选 %d 条，均已推过或无候选）", len(items))
        return 0
    # 手机端页面的公网地址：通知点开、以及「今天不再提醒」按钮都跳到这里。
    # 按钮是 ntfy 的 view 动作（在浏览器里打开），不是 http 动作 —— 后者由 ntfy App
    # 直接发请求，带不上 Cloudflare Access 的 cookie，会被 302 到登录页。
    base = app_settings.public_url("/timelog")
    sent = 0
    for item in todo:
        title, body = remind.message(item)
        # 按钮只用 view 动作：panel. 在 Access 后面，ntfy App 直接发的 http 动作带不上 cookie
        actions = ([{"action": "view", "label": "今天不再提醒", "url": f"{base}?action=mute-today", "clear": True}]
                   if base else None)
        try:
            if item["kind"] == "overtime":
                if base:
                    actions.insert(0, {"action": "view", "label": "暂停", "url": f"{base}?action=pause", "clear": True})
                # high：忘了暂停会把这条记录的数据写坏，要锁屏就能看见、当场处理
                push_service.send_note(title, body, topic=app_settings.timelog_ntfy_topic,
                                       priority="high", click_url=base, actions=actions)
            else:
                # low：不响不震。空闲提醒是「路过看见就行」，响了就是催（需求 3.12）
                push_service.send_note(title, body, topic=app_settings.timelog_ntfy_topic,
                                       priority="low", click_url=base, actions=actions)
        except push_service.PushServiceError as e:
            log.warning("timelog 提醒推送失败（下一轮重试）：%s", e)
            continue
        repo.mark_sent(item["key"], now)
        sent += 1
    log.info("timelog 提醒：推送 %d 条", sent)
    return sent
