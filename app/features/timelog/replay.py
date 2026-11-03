"""计时：操作日志 → 记录 / 快捷按钮的重放规则（纯函数，不碰 DB）。

══════════════════════════════════════════════════════════════════════
 为什么存操作而不是存记录
══════════════════════════════════════════════════════════════════════

手机会离线，操作会晚到、乱序到、重复到；网页也会编辑同一条记录。如果直接
存「记录的当前状态」，每一种到达顺序都要单独写合并规则。改成**只存不可变的
操作**、记录状态由「按时间重放该记录的全部操作」得到，以下几件事都由同一条
规则保证：

  - **重复同步**：操作 id 唯一，重复插入直接忽略，重放结果不变；
  - **乱序到达**：每次都重放该记录的全部操作（按 ts 排序），先到后到结果一样。
    `pause` 先于 `start` 到达时记录暂时不存在，等 `start` 到了再重放就对了；
  - **取消**：`cancel` 是墓碑。之后对这条记录的任何操作都被忽略，所以晚到的
    重复 `start` 也救不活它（需求 3.10：取消即作废、不留记录）；
  - **网页编辑**：也只是一条操作（`edit`/`set_segments`/…），与手机走同一条路。

时间戳 `ts` 由**产生操作的那一端在操作当下生成**（手机端：点下去的那一刻），
服务器绝不改写（需求 §5）。全部用 epoch 毫秒整数，换算成 datetime 是仓储层的事。

══════════════════════════════════════════════════════════════════════
 操作种类
══════════════════════════════════════════════════════════════════════

    手机：start pause resume end cancel edit
    网页：create（补记 / 拆分出的后半段） set_segments delete edit
    按钮：button.put button.delete（手机长按编辑也要能离线，所以同样走操作日志）

一键切换不靠服务端推断：手机**显式**写一条 `pause`（与新 `start` 同一 ts）。
所以「同一时刻只有一个进行中」由唯一的计时端（手机）保证，这里不做跨记录裁决。

重放时分两类「不生效」：
  - **静默忽略**：状态上的空操作（对已暂停的再暂停、对墓碑的任何操作）——
    重复点击、乱序到达都会产生它们，不是问题；
  - **冲突**（进 `conflicts`）：操作本身成立但与时间线矛盾（暂停时刻早于这一段的
    开始、改时间段的数据不合法）。它们留在操作日志里，记录上标出来给网页看。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

RECORD_KINDS = ("start", "pause", "resume", "end", "cancel", "edit",
                "create", "set_segments", "delete")
BUTTON_KINDS = ("button.put", "button.delete")
ALL_KINDS = RECORD_KINDS + BUTTON_KINDS

# 记录上可被 start/create/edit 写入的字段
RECORD_FIELDS = ("name", "type_id", "button_id", "note", "estimate", "estimate_source")
# edit 不许改 button_id：它决定「这次耗时算进哪个按钮的历史」，事后改它没有使用场景
EDITABLE = ("name", "type_id", "note", "estimate", "estimate_source")
BUTTON_FIELDS = ("name", "type_id", "guess_first", "initial_estimate", "order")

# 只有 'guess'（用户事前自己填的）进估算偏差统计。网页事后补/改的预估标 'late'，
# 留着看但不计入 —— 事后的「预估」不是预估（docs/timelog.md）。
ESTIMATE_SOURCES = ("guess", "late")

TOMBSTONES = ("cancelled", "deleted")
ACTIVE = ("running", "paused")

MIN_TS = 1_577_836_800_000          # 2020-01-01：早于它的 ts 一定是坏时钟
FUTURE_SLACK_MS = 60_000            # 网页编辑时间段：允许比服务器快 1 分钟
MAX_FUTURE_MS = 86_400_000          # 手机 ts 比服务器快一天以上 → 坏时钟，拒收
MAX_NAME = 200
MAX_NOTE = 5000
MAX_ESTIMATE_MIN = 24 * 60
MAX_SEGMENTS = 500


class SegmentError(ValueError):
    """时间段不合法。消息直接给人看。"""


# ─────────────────────────── 操作形状校验 ───────────────────────────

def _check_fields(data: dict, allowed: Iterable[str]) -> Optional[str]:
    extra = set(data) - set(allowed)
    if extra:
        return f"不认识的字段：{', '.join(sorted(extra))}"
    if "name" in data and data["name"] is not None:
        if not isinstance(data["name"], str) or len(data["name"]) > MAX_NAME:
            return "name 必须是不超过 200 字的字符串"
    if "note" in data and data["note"] is not None:
        if not isinstance(data["note"], str) or len(data["note"]) > MAX_NOTE:
            return "note 必须是不超过 5000 字的字符串"
    for key in ("type_id", "button_id"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            return f"{key} 必须是字符串"
    if "estimate" in data and data["estimate"] is not None:
        est = data["estimate"]
        if isinstance(est, bool) or not isinstance(est, int) or not 1 <= est <= MAX_ESTIMATE_MIN:
            return "estimate 必须是 1–1440 的整数（分钟）"
    if "estimate_source" in data and data["estimate_source"] is not None:
        if data["estimate_source"] not in ESTIMATE_SOURCES:
            return f"estimate_source 只能是 {'/'.join(ESTIMATE_SOURCES)}"
    return None


def _check_segments_shape(segs: Any) -> Optional[str]:
    if not isinstance(segs, list) or not segs or len(segs) > MAX_SEGMENTS:
        return "segments 必须是非空列表"
    for g in segs:
        if not isinstance(g, dict) or set(g) - {"s", "e"} or "s" not in g:
            return "每个时间段形如 {s, e}"
        for k in ("s", "e"):
            v = g.get(k)
            if v is None and k == "e":
                continue
            if isinstance(v, bool) or not isinstance(v, int):
                return "时间段的 s/e 必须是毫秒整数"
    return None


def validate_op(op: dict, now_ms: int) -> Optional[str]:
    """操作的**形状**是否合法。返回拒收理由，合法返回 None。

    只查形状、不查与现有记录的关系 —— 后者是重放的事（乱序时现在不合理的
    操作，等前面的操作到齐后可能就合理了）。被拒收的操作不入库，客户端应
    丢弃它们（重试也不会成功）。
    """
    oid, ts, kind, rid = op.get("id"), op.get("ts"), op.get("kind"), op.get("rid")
    if not isinstance(oid, str) or not 8 <= len(oid) <= 64:
        return "id 必须是 8–64 字符的字符串"
    if not isinstance(rid, str) or not 8 <= len(rid) <= 64:
        return "rid 必须是 8–64 字符的字符串"
    if kind not in ALL_KINDS:
        return f"不认识的操作：{kind}"
    if isinstance(ts, bool) or not isinstance(ts, int):
        return "ts 必须是毫秒整数"
    if ts < MIN_TS or ts > now_ms + MAX_FUTURE_MS:
        return "ts 超出合理范围（手机时钟可能不对）"
    data = op.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return "data 必须是对象"

    if kind == "start":
        return _check_fields(data, RECORD_FIELDS)
    if kind == "edit":
        return _check_fields(data, EDITABLE) or (None if data else "edit 没有任何字段")
    if kind == "create":
        rest = {k: v for k, v in data.items() if k != "segments"}
        return _check_fields(rest, RECORD_FIELDS) or _check_segments_shape(data.get("segments"))
    if kind == "set_segments":
        if set(data) != {"segments"}:
            return "set_segments 只带 segments"
        return _check_segments_shape(data["segments"])
    if kind == "button.put":
        return _check_button(data)
    # pause / resume / end / cancel / delete / button.delete：不带数据
    return "这个操作不带 data" if data else None


def _check_button(data: dict) -> Optional[str]:
    if set(data) - set(BUTTON_FIELDS):
        return f"不认识的字段：{', '.join(sorted(set(data) - set(BUTTON_FIELDS)))}"
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        return "按钮必须有名称（不超过 200 字）"
    if data.get("type_id") is not None and not isinstance(data["type_id"], str):
        return "type_id 必须是字符串"
    if not isinstance(data.get("guess_first", False), bool):
        return "guess_first 必须是布尔值"
    est = data.get("initial_estimate")
    if est is not None and (isinstance(est, bool) or not isinstance(est, int)
                            or not 1 <= est <= MAX_ESTIMATE_MIN):
        return "initial_estimate 必须是 1–1440 的整数（分钟）"
    order = data.get("order", 0)
    if isinstance(order, bool) or not isinstance(order, (int, float)):
        return "order 必须是数字"
    return None


# ─────────────────────────── 时间段 ───────────────────────────

def validate_segments(segs: list[dict], *, open_tail: bool,
                      now_ms: Optional[int] = None) -> list[dict]:
    """时间段的语义校验，返回规整后的副本。

    规则：非空；s < e；按时间排序且互不重叠（首尾相接可以）；
    `open_tail=True`（记录在计时中）时最后一段必须没有结束时刻，其余情况每段都要有。
    给了 now_ms 时不许写到未来（网页手滑把 14:00 写成 41:00 这类）。
    """
    if _check_segments_shape(segs):
        raise SegmentError(_check_segments_shape(segs))
    out = [{"s": g["s"], "e": g.get("e")} for g in segs]
    for i, g in enumerate(out):
        last = i == len(out) - 1
        if g["e"] is None:
            if not (last and open_tail):
                raise SegmentError("只有计时中的记录，最后一段才能没有结束时刻")
        elif g["s"] >= g["e"]:
            raise SegmentError("每段的开始必须早于结束")
        if i and g["s"] < out[i - 1]["e"]:
            raise SegmentError("时间段必须按顺序排列且互不重叠")
        if now_ms is not None and max(g["s"], g["e"] or 0) > now_ms + FUTURE_SLACK_MS:
            raise SegmentError("时间不能晚于现在")
    if open_tail and out[-1]["e"] is not None:
        raise SegmentError("计时中的记录，最后一段不能有结束时刻")
    return out


def split_segments(segs: list[dict], at: int) -> tuple[list[dict], list[dict]]:
    """在时刻 `at` 把一组（全部已结束的）时间段切成前后两组。

    `at` 落在某一段中间就把那段切开；落在暂停的空隙里就从空隙分开。
    必须严格落在整体跨度之内，否则有一边是空的。
    """
    if any(g["e"] is None for g in segs):
        raise SegmentError("计时中的记录不能拆分")
    if not segs[0]["s"] < at < segs[-1]["e"]:
        raise SegmentError("拆分时刻必须落在记录的开始与结束之间")
    left, right = [], []
    for g in segs:
        if g["e"] <= at:
            left.append(dict(g))
        elif g["s"] >= at:
            right.append(dict(g))
        else:
            left.append({"s": g["s"], "e": at})
            right.append({"s": at, "e": g["e"]})
    return left, right


def coalesce(segs: list[dict]) -> list[dict]:
    """合并两条记录的时间段：排序，重叠或首尾相接的并成一段。

    记录之间允许重叠（主任务挂着时补记一段别的），所以合并时可能重叠 ——
    重叠部分只算一次，这才是「这两件事合起来花了多久」。
    """
    out: list[dict] = []
    for g in sorted(segs, key=lambda x: x["s"]):
        if out and g["s"] <= out[-1]["e"]:
            out[-1]["e"] = max(out[-1]["e"], g["e"])
        else:
            out.append(dict(g))
    return out


# ─────────────────────────── 重放 ───────────────────────────

def sort_key(op: dict) -> tuple:
    # seq 是服务端收到的先后（同一 ts 的多条网页操作靠它定序），id 兜底保证全序
    return (op["ts"], op.get("seq", 0), op["id"])


def _conflict(rec: dict, op: dict, reason: str) -> None:
    rec["conflicts"].append({"op_id": op["id"], "kind": op["kind"], "ts": op["ts"], "reason": reason})


def replay_record(ops: Iterable[dict]) -> Optional[dict]:
    """重放一条记录的全部操作。记录从未被创建（只有零散的后续操作）返回 None。"""
    rec: Optional[dict] = None
    for op in sorted(ops, key=sort_key):
        kind, ts, data = op["kind"], op["ts"], op.get("data") or {}
        if kind not in RECORD_KINDS:
            continue
        if rec is None:
            if kind == "start":
                rec = _new_record(op["rid"], data, [{"s": ts, "e": None}], "running", ts)
            elif kind == "create":
                try:
                    segs = validate_segments(data["segments"], open_tail=False)
                except SegmentError:
                    continue      # 创建本身不合法：当它不存在（网页提交前已经校验过）
                fields = {k: v for k, v in data.items() if k != "segments"}
                rec = _new_record(op["rid"], fields, segs, "done", ts)
            # 其余操作早于创建：乱序到达，等 start 到了重放就对了
            continue

        rec["updated_ts"] = max(rec["updated_ts"], ts)
        status, segs = rec["status"], rec["segments"]
        if status in TOMBSTONES:
            continue
        if kind in ("start", "create"):
            continue                     # 同一 rid 重复创建：以第一次为准
        if kind == "cancel":
            rec["status"] = "cancelled"
        elif kind == "delete":
            rec["status"] = "deleted"
        elif kind == "edit":
            for k in EDITABLE:
                if k in data:
                    rec[k] = data[k]
        elif kind == "pause":
            if status == "running":
                if ts < segs[-1]["s"]:
                    _conflict(rec, op, "暂停时刻早于这一段的开始")
                else:
                    segs[-1]["e"] = ts
                    rec["status"] = "paused"
        elif kind == "resume":
            if status == "paused":
                if ts < segs[-1]["e"]:
                    _conflict(rec, op, "恢复时刻早于上一次暂停")
                else:
                    segs.append({"s": ts, "e": None})
                    rec["status"] = "running"
        elif kind == "end":
            if status == "running":
                if ts < segs[-1]["s"]:
                    _conflict(rec, op, "结束时刻早于这一段的开始")
                    continue
                segs[-1]["e"] = ts
                rec["status"] = "done"
            elif status == "paused":
                rec["status"] = "done"
        elif kind == "set_segments":
            try:
                rec["segments"] = validate_segments(data["segments"], open_tail=status == "running")
            except SegmentError as e:
                _conflict(rec, op, f"改时间段未生效：{e}")
    return rec


def _new_record(rid: str, data: dict, segs: list[dict], status: str, ts: int) -> dict:
    rec = {k: data.get(k) for k in RECORD_FIELDS}
    rec["name"] = (rec["name"] or "").strip() or None      # 空名 = 未命名
    rec.update(id=rid, status=status, segments=segs, created_ts=ts, updated_ts=ts, conflicts=[])
    return rec


def derived(rec: dict, now_ms: int) -> dict:
    """记录的派生量：实际耗时（排除暂停）、整体跨度。计时中的按 now 算到此刻。"""
    segs = rec["segments"]
    return {
        "actual_ms": sum((g["e"] if g["e"] is not None else now_ms) - g["s"] for g in segs),
        "span_start": segs[0]["s"],
        "span_end": segs[-1]["e"],          # 计时中为 None
    }


def closed_actual_ms(rec: dict) -> Optional[int]:
    """可以落库的实际耗时：只有每段都已结束才有定值。

    有开放段的不只是计时中 —— **计时中被取消**的墓碑也保留着开放的最后一段
    （取消不补结束时刻）。只按 status 判断会在这里拿 None 去减，整批同步 500。
    """
    segs = rec["segments"]
    if segs[-1]["e"] is None:
        return None
    return sum(g["e"] - g["s"] for g in segs)


def replay_button(ops: Iterable[dict]) -> Optional[dict]:
    """快捷按钮：按时间最后一次 put 为准；delete 是墓碑（之后的 put 不生效）。"""
    btn: Optional[dict] = None
    for op in sorted(ops, key=sort_key):
        if op["kind"] == "button.delete":
            if btn is not None:
                btn["deleted"] = True
            else:
                btn = {"id": op["rid"], "deleted": True, "name": None}
        elif op["kind"] == "button.put":
            if btn is not None and btn.get("deleted"):
                continue
            d = op.get("data") or {}
            btn = {
                "id": op["rid"],
                "name": d["name"].strip(),
                "type_id": d.get("type_id"),
                "guess_first": bool(d.get("guess_first", False)),
                "initial_estimate": d.get("initial_estimate"),
                "order": d.get("order", 0),
                "deleted": False,
            }
    return btn if btn is None or btn.get("name") is not None else None


# ─────────────────────────── 参考时长 ───────────────────────────

def _half_up(x: float) -> int:
    # 不用 round()：Python 是银行家舍入（round(2.5)==2），手机端 JS 的 Math.round(2.5)==3，
    # 两端会在 .5 上给出不同的参考时长（12.5 分 → 10 还是 15）
    return math.floor(x + 0.5)


def round_ref(minutes: float) -> int:
    """10 分以下精确到分，以上取整到 5 分 —— 「通常 23–37 分」是假精确。

    与 timelog.html 的 roundRef() 必须逐值一致。
    """
    if minutes < 10:
        return max(1, _half_up(minutes))
    return _half_up(minutes / 5) * 5


def _quantile(xs: list[float], q: float) -> float:
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def reference(samples_min: list[float]) -> dict:
    """快捷按钮的参考时长（需求 3.7），与手机端 timelog.html 的 reference() 同一套分档。

        n=0 → none；n=1 → last（上次）；n=2–3 → range（最短–最长）；
        n≥4 → iqr（四分位区间：中间一半的记录落在这里，个别异常值拉不动它）。
    """
    xs = sorted(samples_min)
    n = len(xs)
    if n == 0:
        return {"kind": "none", "n": 0, "lo": None, "hi": None}
    if n == 1:
        v = round_ref(xs[0])
        return {"kind": "last", "n": 1, "lo": v, "hi": v}
    if n <= 3:
        return {"kind": "range", "n": n, "lo": round_ref(xs[0]), "hi": round_ref(xs[-1])}
    return {"kind": "iqr", "n": n,
            "lo": round_ref(_quantile(xs, 0.25)), "hi": round_ref(_quantile(xs, 0.75))}
