"""AI 小镇（town）的持久化。

| 集合 | 键 | 职责 |
|---|---|---|
| `town_towns` | `_id` = 镇 id | 镇本身：地点、已完成到第几天、累计花费 |
| `town_residents` | `_id` = 居民 id | 居民：入住时的设定**快照**（不随酒馆变动）+ 手动开关的条目 + 补充说明 |
| `town_days` | `(town_id, day)` 唯一 | 一天的全部产出：计划、场景、状态、花费。**每完成一个调用就落一次库** |
| `town_memories` | `(town_id, rid, day)` 唯一 | 日记 = 居民的持久记忆 |

**居民的「当前状态」不单独存，一律由日记派生**：心情、目标、对别人的印象取
最新一篇日记；地点上的既定事实取已完成的各天场景。这样「回退到第 N 天」只需删掉
N 天之后的 days 与 memories，不存在「删了日记、心情却还停在第 N+3 天」的错位。

唯一索引就是幂等键：断点续跑时已写进去的计划/场景/日记直接跳过，**不会重复
烧 API**，也不会产出两篇同一天的日记。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import ASCENDING, DESCENDING

from app.clients.mongo import get_collection
from app.core.settings import settings


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class TownRepository:
    def __init__(self) -> None:
        db = settings.mongo_default_db
        self.towns = get_collection("town_towns", db)
        self.residents = get_collection("town_residents", db)
        self.days = get_collection("town_days", db)
        self.memories = get_collection("town_memories", db)

        self.residents.create_index([("town_id", ASCENDING), ("order", ASCENDING)])
        self.days.create_index([("town_id", ASCENDING), ("day", ASCENDING)], unique=True)
        self.memories.create_index(
            [("town_id", ASCENDING), ("rid", ASCENDING), ("day", ASCENDING)], unique=True,
        )

    # ── 镇 ──

    def create_town(self, doc: dict) -> str:
        doc = {**doc, "_id": new_id(), "day": 0, "created_at": now(),
               "cost": {"yuan": 0.0, "input": 0, "cache_hit": 0, "output": 0, "calls": 0}}
        self.towns.insert_one(doc)
        return doc["_id"]

    def get_town(self, town_id: str) -> Optional[dict]:
        return self.towns.find_one({"_id": town_id})

    def list_towns(self) -> list[dict]:
        return list(self.towns.find({}, {"places": 0}).sort("created_at", DESCENDING))

    def set_town_day(self, town_id: str, day: int) -> None:
        self.towns.update_one({"_id": town_id}, {"$set": {"day": day}})

    def add_cost(self, town_id: str, day: int, c: dict) -> None:
        inc = {f"cost.{k}": v for k, v in c.items()}
        self.towns.update_one({"_id": town_id}, {"$inc": inc})
        self.days.update_one({"town_id": town_id, "day": day},
                             {"$inc": {f"cost.{k}": v for k, v in c.items()}})

    def update_town(self, town_id: str, fields: dict) -> None:
        self.towns.update_one({"_id": town_id}, {"$set": fields})

    def delete_town(self, town_id: str) -> None:
        for col in (self.residents, self.days, self.memories):
            col.delete_many({"town_id": town_id})
        self.towns.delete_one({"_id": town_id})

    # ── 居民 ──

    def add_resident(self, doc: dict) -> str:
        doc = {**doc, "_id": new_id(), "snapshot_at": now()}
        self.residents.insert_one(doc)
        return doc["_id"]

    def residents_of(self, town_id: str) -> list[dict]:
        return list(self.residents.find({"town_id": town_id}).sort("order", ASCENDING))

    def get_resident(self, rid: str) -> Optional[dict]:
        return self.residents.find_one({"_id": rid})

    def update_resident(self, rid: str, fields: dict) -> None:
        self.residents.update_one({"_id": rid}, {"$set": fields})

    # ── 天 ──

    def get_day(self, town_id: str, day: int) -> Optional[dict]:
        return self.days.find_one({"town_id": town_id, "day": day})

    def ensure_day(self, town_id: str, day: int) -> dict:
        self.days.update_one(
            {"town_id": town_id, "day": day},
            {"$setOnInsert": {"town_id": town_id, "day": day, "created_at": now(),
                              "plans": {}, "scenes": {}, "status": "running",
                              "cost": {"yuan": 0.0, "input": 0, "cache_hit": 0, "output": 0, "calls": 0}}},
            upsert=True,
        )
        return self.get_day(town_id, day)

    def set_day(self, town_id: str, day: int, fields: dict) -> None:
        self.days.update_one({"town_id": town_id, "day": day}, {"$set": fields})

    def days_of(self, town_id: str, *, upto: Optional[int] = None) -> list[dict]:
        q: dict[str, Any] = {"town_id": town_id}
        if upto is not None:
            q["day"] = {"$lte": upto}
        return list(self.days.find(q).sort("day", ASCENDING))

    def day_summaries(self, town_id: str) -> list[dict]:
        """时间线用：不带场景正文。scenes 是 {place_id: scene} 的动态键，Mongo 投影
        够不着，取回来在这里剥（一个镇也就几十上百天）。"""
        out = []
        for d in self.days.find({"town_id": town_id}, {"plans": 0}).sort("day", ASCENDING):
            d["scenes"] = {
                pid: {k: s.get(k) for k in ("place", "title", "summary", "members", "error")}
                for pid, s in (d.get("scenes") or {}).items()
            }
            out.append(d)
        return out

    def interrupt_running(self, reason: str) -> int:
        r = self.days.update_many({"status": "running"},
                                  {"$set": {"status": "interrupted", "error": reason}})
        return r.modified_count

    # ── 记忆 ──

    def put_memory(self, town_id: str, rid: str, day: int, doc: dict) -> None:
        self.memories.update_one(
            {"town_id": town_id, "rid": rid, "day": day},
            {"$set": {**doc, "town_id": town_id, "rid": rid, "day": day, "written_at": now()}},
            upsert=True,
        )

    def has_memory(self, town_id: str, rid: str, day: int) -> bool:
        return self.memories.count_documents({"town_id": town_id, "rid": rid, "day": day}, limit=1) > 0

    def memories_of(self, town_id: str, rid: str, *, before: Optional[int] = None) -> list[dict]:
        q: dict[str, Any] = {"town_id": town_id, "rid": rid}
        if before is not None:
            q["day"] = {"$lt": before}
        return list(self.memories.find(q).sort("day", ASCENDING))

    def latest_memory(self, town_id: str, rid: str) -> Optional[dict]:
        return self.memories.find_one({"town_id": town_id, "rid": rid}, sort=[("day", DESCENDING)])

    # ── 回退 ──

    def rollback(self, town_id: str, to_day: int) -> dict:
        d = self.days.delete_many({"town_id": town_id, "day": {"$gt": to_day}}).deleted_count
        m = self.memories.delete_many({"town_id": town_id, "day": {"$gt": to_day}}).deleted_count
        self.towns.update_one({"_id": town_id}, {"$set": {"day": to_day}})
        return {"days": d, "memories": m}


_repo: Optional[TownRepository] = None


def get_town_repo() -> TownRepository:
    """懒加载单例 —— 建索引要连 Mongo，不该在 import 期做。"""
    global _repo
    if _repo is None:
        _repo = TownRepository()
    return _repo
