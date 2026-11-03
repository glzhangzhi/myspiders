"""统一 LLM 用量的 Mongo 持久化（集合 `llm_usage` / `llm_usage_state`）。

一次模型调用一条文档，只存 token 与 UTC 时刻；金额读取时由 `llm_pricing` 计算。
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pymongo import ASCENDING, UpdateOne

from app.clients.mongo import get_collection
from app.core.logging import get_logger
from app.core.settings import settings
from app.features.llm_usage.pricing import DEEPSEEK_PROVIDERS

logger = get_logger("LLM_USAGE_REPO")

# 汇总时只取计价需要的字段，省掉 _id 等
_FIELDS = {"_id": 0, "source": 1, "ts": 1, "provider": 1, "model": 1, "session_id": 1,
           "miss": 1, "hit": 1, "cache_write": 1, "output": 1}


class LlmUsageRepository:
    def __init__(self) -> None:
        self.col = get_collection("llm_usage", settings.mongo_default_db)
        self.state = get_collection("llm_usage_state", settings.mongo_default_db)
        # DSH sessionId → cwd（会话创建时写死，永不失效）。_id 就是 sessionId。
        self.sessions = get_collection("llm_usage_sessions", settings.mongo_default_db)
        try:
            self.col.create_index("id", unique=True)
            self.col.create_index([("ts", ASCENDING)])
            self.col.create_index([("source", ASCENDING), ("ts", ASCENDING)])
        except Exception:
            logger.warning("创建 llm_usage 索引失败（忽略，下次启动重试）")

    def upsert_many(self, docs: list[dict]) -> int:
        """按 id 幂等写入，返回新增 + 修改的条数。"""
        if not docs:
            return 0
        res = self.col.bulk_write(
            [UpdateOne({"id": d["id"]}, {"$set": d}, upsert=True) for d in docs],
            ordered=False,
        )
        return res.upserted_count + res.modified_count

    def get_offset(self, key: str) -> int:
        doc = self.state.find_one({"_id": key})
        return int(doc.get("offset", 0)) if doc else 0

    def set_offset(self, key: str, offset: int) -> None:
        self.state.update_one(
            {"_id": key},
            {"$set": {"offset": offset, "updated_at": dt.datetime.now(dt.timezone.utc)}},
            upsert=True,
        )

    def docs_between(self, start: dt.datetime, end: dt.datetime):
        """[start, end) 内的全部调用（游标）。一个月量级为数万条，内存里计价足够快。"""
        return self.col.find({"ts": {"$gte": start, "$lt": end}}, _FIELDS)

    def known_session_ids(self) -> set[str]:
        return {d["_id"] for d in self.sessions.find({}, {"_id": 1})}

    def upsert_sessions(self, docs: list[dict]) -> None:
        if docs:
            self.sessions.bulk_write(
                [UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True) for d in docs], ordered=False)

    def session_cwds(self) -> dict[str, str]:
        return {d["_id"]: d.get("cwd") for d in self.sessions.find({}, {"cwd": 1})}

    def months(self, tz: str) -> list[str]:
        """有数据的月份（按 `tz` 的自然月），新的在前。"""
        rows = self.col.aggregate([
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$ts", "timezone": tz}}}},
            {"$sort": {"_id": -1}},
        ])
        return [r["_id"] for r in rows]

    def last_seen(self) -> dict[str, Optional[str]]:
        """每个来源最后一条记录的时间（不限月份），用于看数据有没有断档。"""
        out: dict[str, Optional[str]] = {}
        # 与 llm_pricing.row_key 同一分行：DSH 里走 DeepSeek 的单独成一行
        key = {"$cond": [
            {"$and": [{"$eq": ["$source", "dsh"]},
                      {"$in": ["$provider", sorted(DEEPSEEK_PROVIDERS)]}]},
            "dsh-deepseek", "$source"]}
        for r in self.col.aggregate([{"$group": {"_id": key, "last": {"$max": "$ts"}}}]):
            last = r["last"]
            if isinstance(last, dt.datetime) and last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            out[r["_id"]] = last.isoformat() if last else None
        return out


llm_usage_repo = LlmUsageRepository()
