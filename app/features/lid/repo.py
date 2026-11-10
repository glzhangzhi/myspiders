"""入籍考试刷题的持久化（单用户）。

- `lid_progress`：每题一条，主键 qid（字段见 srs.new_state）
- `lid_answers` ：作答日志 {qid, mode, chosen, correct, at, day}；day = 德国日历日，按天统计用
- `lid_exams`   ：模拟考 {qids, answers, started_at, finished_at, score, passed}

集合句柄与索引懒加载：导入本模块不连库（测试与装配检查不碰 DB）。
"""

from __future__ import annotations

from datetime import datetime
from functools import cached_property
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId

from app.clients.mongo import get_collection
from app.core.settings import settings
from app.features.lid.srs import new_state

_FIELDS = ("qid", "level", "due", "streak", "seen", "correct", "wrong", "last_at", "wrongbook", "starred", "intro_on")


class LidRepo:
    @cached_property
    def progress(self):
        c = get_collection("lid_progress", settings.mongo_default_db)
        c.create_index("qid", unique=True)
        return c

    @cached_property
    def answers(self):
        c = get_collection("lid_answers", settings.mongo_default_db)
        c.create_index("day")
        c.create_index("qid")
        return c

    @cached_property
    def exams(self):
        c = get_collection("lid_exams", settings.mongo_default_db)
        c.create_index("started_at")
        return c

    # ── 进度 ──

    def all_progress(self) -> list[dict]:
        return list(self.progress.find({}, {"_id": 0}))

    def get(self, qid: str) -> Optional[dict]:
        return self.progress.find_one({"qid": qid}, {"_id": 0})

    def save(self, state: dict) -> dict:
        doc = {k: state.get(k) for k in _FIELDS}
        self.progress.update_one({"qid": doc["qid"]}, {"$set": doc}, upsert=True)
        return doc

    def set_flag(self, qid: str, field: str, value: bool) -> dict:
        assert field in ("starred", "wrongbook")
        base = {k: v for k, v in new_state(qid).items() if k not in (field, "qid")}
        self.progress.update_one({"qid": qid}, {"$set": {field: bool(value)}, "$setOnInsert": base}, upsert=True)
        return self.get(qid)

    # ── 作答日志 ──

    def log_answers(self, docs: list[dict]) -> None:
        if docs:
            self.answers.insert_many([dict(d) for d in docs])

    def count_on(self, day: str) -> int:
        return self.answers.count_documents({"day": day})

    def per_day(self, since_day: str) -> dict[str, dict]:
        rows = self.answers.aggregate([
            {"$match": {"day": {"$gte": since_day}}},
            {"$group": {"_id": "$day", "n": {"$sum": 1}, "ok": {"$sum": {"$cond": ["$correct", 1, 0]}}}},
        ])
        return {r["_id"]: {"n": r["n"], "ok": r["ok"]} for r in rows}

    def totals(self) -> dict:
        rows = list(self.answers.aggregate([
            {"$group": {"_id": None, "n": {"$sum": 1}, "ok": {"$sum": {"$cond": ["$correct", 1, 0]}}}},
        ]))
        return {"n": rows[0]["n"], "ok": rows[0]["ok"]} if rows else {"n": 0, "ok": 0}

    # ── 模拟考 ──

    def create_exam(self, qids: list[str], started_at: datetime) -> str:
        r = self.exams.insert_one({"qids": qids, "answers": None, "started_at": started_at,
                                   "finished_at": None, "score": None, "passed": None})
        return str(r.inserted_id)

    def get_exam(self, exam_id: str) -> Optional[dict]:
        try:
            oid = ObjectId(exam_id)
        except (InvalidId, TypeError):
            return None
        return self.exams.find_one({"_id": oid})

    def finish_exam(self, exam_id: str, answers: list, finished_at: datetime, score: int, passed: bool) -> bool:
        """只在尚未交卷时写入（防重复交卷把进度写两遍）。返回是否由本次写入。"""
        r = self.exams.update_one(
            {"_id": ObjectId(exam_id), "finished_at": None},
            {"$set": {"answers": answers, "finished_at": finished_at, "score": score, "passed": passed}},
        )
        return r.modified_count == 1

    def list_exams(self, limit: int = 100) -> list[dict]:
        return list(self.exams.find({"finished_at": {"$ne": None}}).sort("started_at", -1).limit(limit))

    def delete_unfinished_before(self, before: datetime) -> None:
        self.exams.delete_many({"finished_at": None, "started_at": {"$lt": before}})


repo = LidRepo()
