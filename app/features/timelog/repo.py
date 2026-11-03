"""计时（timelog）的持久化。

| 集合 | 键 | 职责 |
|---|---|---|
| `timelog_ops` | `_id` = 操作 id | **唯一事实来源**。不可变，只增不改；`_id` 唯一 = 重复同步天然幂等 |
| `timelog_records` | `_id` = 记录 id | 由 ops 重放出的**物化视图**，可随时整体重建（`rebuild_all`） |
| `timelog_buttons` | `_id` = 按钮 id | 同上，快捷按钮的物化视图 |
| `timelog_types` | `_id` = 类型 id | 类型。只在网页上改、不需要离线，所以直接增改，不走操作日志 |
| `timelog_settings` | `key="current"` 单文档 | 一键切换行为、提醒阈值、静音时段 |

规则见 `app/features/timelog/replay.py` 的模块 docstring。存储约定：ops 里的 `ts`
保持手机给的**毫秒整数**原样（那是客户端说的事实，不做任何换算）；物化记录额外
存 `span_start`/`span_end` 两个 UTC datetime 只为按时间窗查询建索引。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from pymongo import ASCENDING, UpdateOne
from pymongo.errors import BulkWriteError

from app.clients.mongo import get_collection
from app.core.settings import settings
from app.features.timelog import replay as R

# 首次使用时播种的类型（需求 3.3 的例子）。之后以库为准，改它不影响已有数据。
DEFAULT_TYPES = ("写接口", "修 Bug", "写测试", "开会", "通勤", "吃饭", "家务", "娱乐", "睡眠")
N_COLORS = 8          # 与 timelog.html 的 --t0…--t7 对应

DEFAULT_SETTINGS: dict[str, Any] = {
    # 开始新任务时，正在计时的那条：pause（默认，2026-11 机主定）或 end
    "switch_behavior": "pause",
    # 空闲提醒：这么久没有任何计时中的记录就提醒（需求 3.6）
    "idle_remind_minutes": 30,
    # 超时提醒：有预估的超过 预估×倍数，没预估的超过上限（需求 3.6）
    "overtime_ratio": 1.5,
    "overtime_cap_minutes": 180,
    # 静音（需求 3.12：晚上和周末默认静音）。本地时间 HH:MM，跨午夜表示到次日
    "quiet_start": "20:00",
    "quiet_end": "08:00",
    "quiet_weekends": True,
    "timezone": "Europe/Berlin",
}


def _ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class TimelogRepository:
    def __init__(self) -> None:
        db = settings.mongo_default_db
        self.ops = get_collection("timelog_ops", db)
        self.records = get_collection("timelog_records", db)
        self.buttons = get_collection("timelog_buttons", db)
        self.types = get_collection("timelog_types", db)
        self.settings = get_collection("timelog_settings", db)
        self.ops.create_index([("rid", ASCENDING), ("ts", ASCENDING)])
        self.ops.create_index([("source", ASCENDING), ("ts", ASCENDING)])
        self.records.create_index([("span_start", ASCENDING)])
        self.records.create_index([("status", ASCENDING), ("span_end", ASCENDING)])
        self.records.create_index([("button_id", ASCENDING), ("status", ASCENDING)])
        self.settings.create_index("key", unique=True)

    # ─────────────────────── 操作日志 ───────────────────────

    def insert_ops(self, ops: list[dict], source: str) -> tuple[list[str], list[str]]:
        """插入一批已通过形状校验的操作，返回 (新插入的 id, 重复的 id)。

        `_id` 唯一索引就是去重：手机重发同一批（上次响应丢了）时，已有的报
        DuplicateKeyError 被归为「重复」，结果与第一次完全一样。
        """
        if not ops:
            return [], []
        received = datetime.now(timezone.utc)
        base = time.time_ns()
        docs = []
        for i, op in enumerate(ops):
            docs.append({
                "_id": op["id"], "ts": op["ts"], "kind": op["kind"], "rid": op["rid"],
                "data": op.get("data") or {},
                # seq：服务端收到的先后，只用于同一 ts 的操作定序（见 timelog_replay.sort_key）
                "seq": base + i,
                "source": source, "received_at": received,
            })
        dup: set[str] = set()
        try:
            self.ops.insert_many(docs, ordered=False)
        except BulkWriteError as e:
            for err in e.details.get("writeErrors", []):
                if err.get("code") != 11000:
                    raise
                dup.add(docs[err["index"]]["_id"])
        inserted = [d["_id"] for d in docs if d["_id"] not in dup]
        return inserted, sorted(dup)

    def ops_for(self, rid: str) -> list[dict]:
        return [self._op_out(d) for d in self.ops.find({"rid": rid})]

    def ops_for_many(self, rids: Iterable[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for d in self.ops.find({"rid": {"$in": list(rids)}}):
            out.setdefault(d["rid"], []).append(self._op_out(d))
        return out

    @staticmethod
    def _op_out(d: dict) -> dict:
        return {"id": d["_id"], "ts": d["ts"], "kind": d["kind"], "rid": d["rid"],
                "data": d.get("data") or {}, "seq": d.get("seq", 0)}

    # ─────────────────────── 物化 ───────────────────────

    def rematerialize(self, rids: Iterable[str]) -> None:
        """重放这些 id（记录或按钮）的全部操作，覆盖物化视图。"""
        rids = list(set(rids))
        if not rids:
            return
        by_rid = self.ops_for_many(rids)
        rec_writes, btn_writes = [], []
        for rid in rids:
            ops = by_rid.get(rid, [])
            if any(o["kind"] in R.BUTTON_KINDS for o in ops):
                btn = R.replay_button(o for o in ops if o["kind"] in R.BUTTON_KINDS)
                if btn is not None:
                    doc = {k: v for k, v in btn.items() if k != "id"}
                    btn_writes.append(UpdateOne({"_id": rid}, {"$set": doc}, upsert=True))
            if any(o["kind"] in R.RECORD_KINDS for o in ops):
                rec = R.replay_record(o for o in ops if o["kind"] in R.RECORD_KINDS)
                if rec is not None:
                    rec_writes.append(UpdateOne({"_id": rid}, {"$set": self._record_doc(rec)}, upsert=True))
        if rec_writes:
            self.records.bulk_write(rec_writes, ordered=False)
        if btn_writes:
            self.buttons.bulk_write(btn_writes, ordered=False)

    @staticmethod
    def _record_doc(rec: dict) -> dict:
        doc = {k: v for k, v in rec.items() if k != "id"}
        segs = rec["segments"]
        doc["span_start"] = _ms_to_dt(segs[0]["s"])
        doc["span_end"] = _ms_to_dt(segs[-1]["e"])          # 计时中为 None
        # 有开放段的耗时随时间变，存了也是错的；读的时候现算（R.derived）
        doc["actual_ms"] = R.closed_actual_ms(rec)
        return doc

    def rebuild_all(self) -> int:
        """从操作日志整体重建两个物化视图（改了重放规则之后跑一次）。"""
        rids = self.ops.distinct("rid")
        self.records.delete_many({})
        self.buttons.delete_many({})
        for i in range(0, len(rids), 500):
            self.rematerialize(rids[i:i + 500])
        return len(rids)

    # ─────────────────────── 记录查询 ───────────────────────

    @staticmethod
    def _rec_out(d: dict) -> dict:
        d = dict(d)
        d["id"] = d.pop("_id")
        d.pop("span_start", None)
        d.pop("span_end", None)
        return d

    def get_record(self, rid: str) -> Optional[dict]:
        d = self.records.find_one({"_id": rid})
        return self._rec_out(d) if d else None

    def active_records(self) -> list[dict]:
        return [self._rec_out(d) for d in self.records.find({"status": {"$in": list(R.ACTIVE)}})]

    def records_between(self, start: datetime, end: datetime) -> list[dict]:
        """与 [start, end) 有交集的记录（不含墓碑）。计时中的 span_end 为空，视为到现在。"""
        q = {
            "status": {"$nin": list(R.TOMBSTONES)},
            "span_start": {"$lt": end},
            "$or": [{"span_end": None}, {"span_end": {"$gt": start}}],
        }
        return [self._rec_out(d) for d in self.records.find(q).sort("span_start", ASCENDING)]

    def unnamed_records(self) -> list[dict]:
        q = {"status": "done", "name": None}
        return [self._rec_out(d) for d in self.records.find(q).sort("span_start", -1)]

    def button_durations_ms(self) -> dict[str, list[int]]:
        """每个按钮的历史实际耗时（只算已完成的）。"""
        out: dict[str, list[int]] = {}
        q = {"status": "done", "button_id": {"$ne": None}}
        for d in self.records.find(q, {"button_id": 1, "actual_ms": 1}):
            if d.get("actual_ms"):
                out.setdefault(d["button_id"], []).append(d["actual_ms"])
        return out

    # ─────────────────────── 按钮 ───────────────────────

    def list_buttons(self) -> list[dict]:
        out = []
        for d in self.buttons.find({"deleted": {"$ne": True}}).sort("order", ASCENDING):
            d = dict(d)
            d["id"] = d.pop("_id")
            out.append(d)
        return out

    # ─────────────────────── 类型 ───────────────────────

    def _seed_types(self) -> None:
        if self.types.estimated_document_count():
            return
        now = datetime.now(timezone.utc)
        self.types.insert_many([
            {"_id": str(uuid.uuid4()), "name": n, "color": i % N_COLORS, "order": i,
             "archived": False, "created_at": now}
            for i, n in enumerate(DEFAULT_TYPES)
        ])

    def list_types(self, include_archived: bool = False) -> list[dict]:
        self._seed_types()
        q = {} if include_archived else {"archived": {"$ne": True}}
        out = []
        for d in self.types.find(q).sort("order", ASCENDING):
            d = dict(d)
            d["id"] = d.pop("_id")
            d.pop("created_at", None)
            out.append(d)
        return out

    def get_type(self, tid: str) -> Optional[dict]:
        return self.types.find_one({"_id": tid})

    def create_type(self, name: str, color: Optional[int]) -> str:
        self._seed_types()
        last = self.types.find_one(sort=[("order", -1)])
        order = (last["order"] + 1) if last else 0
        tid = str(uuid.uuid4())
        self.types.insert_one({
            "_id": tid, "name": name, "order": order, "archived": False,
            "color": order % N_COLORS if color is None else color,
            "created_at": datetime.now(timezone.utc),
        })
        return tid

    def update_type(self, tid: str, fields: dict) -> bool:
        return self.types.update_one({"_id": tid}, {"$set": fields}).matched_count == 1

    def last_phone_ts(self) -> Optional[int]:
        """手机最后一条操作的 ts（空闲提醒的「最后一次动静」）。

        只认手机：网页补记一段昨天的记录不代表此刻在用。
        """
        d = self.ops.find_one({"source": "phone"}, sort=[("ts", -1)])
        return d["ts"] if d else None

    # ─────────────────────── 提醒状态 ───────────────────────
    # 与设置同一个集合、另一条文档：设置是人改的，这些是机器记的（muted_until、
    # 已推过的去重键），混在一起会让「改设置」覆盖掉去重状态。

    def remind_state(self) -> dict:
        doc = self.settings.find_one({"key": "remind"}) or {}
        return {"muted_until": doc.get("muted_until", 0), "sent": doc.get("sent", [])}

    def set_muted_until(self, ms: int) -> None:
        self.settings.update_one({"key": "remind"}, {"$set": {"muted_until": ms}}, upsert=True)

    def mark_sent(self, key: str, at_ms: int, keep: int = 200) -> None:
        self.settings.update_one(
            {"key": "remind"},
            {"$push": {"sent": {"$each": [{"key": key, "at": at_ms}], "$slice": -keep}}},
            upsert=True,
        )

    # ─────────────────────── 设置 ───────────────────────

    def get_settings(self) -> dict:
        doc = self.settings.find_one({"key": "current"}) or {}
        return {k: doc.get(k, v) for k, v in DEFAULT_SETTINGS.items()}

    def update_settings(self, fields: dict) -> dict:
        self.settings.update_one({"key": "current"}, {"$set": fields}, upsert=True)
        return self.get_settings()


timelog_repo: Optional[TimelogRepository] = None


def get_timelog_repo() -> TimelogRepository:
    """懒加载单例 —— 建索引要连 Mongo，不该在 import 期做。"""
    global timelog_repo
    if timelog_repo is None:
        timelog_repo = TimelogRepository()
    return timelog_repo
