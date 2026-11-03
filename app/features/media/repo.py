"""媒体下载的持久化。

| 集合 | `_id` | 职责 |
|---|---|---|
| `media_sources` | `wx:<finder username>` / `yt:<playlist id>` / 未解析的 `wx:pending:<uuid>` | 要盯的作者和列表 |
| `media_items` | `wx:<feed id>` / `yt:<video id>` | 每个视频一条，状态机 pending→downloading→done/failed，或 skipped |
| `media_runs` | ObjectId | 每次检测/下载一条运行记录（含日志尾巴与当前进度） |

两条不变量：
- **检测不改已有条目的状态**：同一个视频再被检测到时只补元数据（`$setOnInsert` 写状态），
  否则「已完成」会被一次检测打回「待下载」，或者用户「跳过」的又冒出来。
- **删来源不删已完成的记录**：done 是「这个文件下过了」的唯一凭据，删掉的话
  重新添加同一个来源会把同样的东西再下一遍。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.clients.mongo import get_collection
from app.features.media import rules as R


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class MediaRepo:
    def __init__(self) -> None:
        self.sources = get_collection("media_sources")
        self.items = get_collection("media_items")
        self.runs = get_collection("media_runs")
        self._indexed = False

    def ensure_indexes(self) -> None:
        if self._indexed:
            return
        self.items.create_index([("status", ASCENDING), ("discovered_at", DESCENDING)])
        self.items.create_index([("source_id", ASCENDING), ("status", ASCENDING)])
        self.runs.create_index([("started_at", DESCENDING)])
        self._indexed = True

    # ── sources ──────────────────────────────────────────────────────────

    def list_sources(self) -> list[dict]:
        return list(self.sources.find().sort([("platform", ASCENDING), ("created_at", ASCENDING)]))

    def get_source(self, sid: str) -> Optional[dict]:
        return self.sources.find_one({"_id": sid})

    def add_source(self, parsed: R.ParsedSource, name: str = "", mode: Optional[str] = None,
                   extra: Optional[dict] = None) -> dict:
        if parsed.key:
            sid = R.source_id(parsed.platform, parsed.key)
            status = "active"
        else:
            sid = f"{parsed.platform}:pending:{uuid.uuid4().hex[:10]}"
            status = "unresolved"
        if mode is None:
            mode = "all" if parsed.platform == "yt" else "new_only"
        doc = {
            "_id": sid,
            "platform": parsed.platform,
            "key": parsed.key,
            "input": parsed.input,
            "name": name or (parsed.input if parsed.platform == "wx" else ""),
            "status": status,
            "mode": mode,
            "baseline_done": False,
            "candidates": [],
            "created_at": _now(),
            "last_scan_at": None,
            "last_scan_error": None,
            "last_new_count": None,
            **(extra or {}),
        }
        existing = self.sources.find_one({"_id": sid})
        if existing:
            return existing
        self.sources.insert_one(doc)
        return doc

    def patch_source(self, sid: str, fields: dict) -> Optional[dict]:
        return self.sources.find_one_and_update(
            {"_id": sid}, {"$set": fields}, return_document=ReturnDocument.AFTER)

    def rekey_source(self, old_id: str, username: str, nickname: str, head_url: str = "") -> dict:
        """未解析的视频号来源绑定到真实 username（`_id` 不能改，只能换一条）。

        目标 id 已存在（用户把同一个人加了两次）就合并到已有那条、删掉占位。
        """
        old = self.sources.find_one({"_id": old_id}) or {}
        new_id = R.source_id("wx", username)
        if old_id == new_id:
            return self.patch_source(old_id, {"name": nickname, "head_url": head_url, "status": "active",
                                              "candidates": []}) or old
        existing = self.sources.find_one({"_id": new_id})
        if not existing:
            doc = {**old, "_id": new_id, "key": username, "name": nickname or old.get("name", ""),
                   "head_url": head_url, "status": "active", "candidates": []}
            self.sources.insert_one(doc)
            existing = doc
        self.sources.delete_one({"_id": old_id})
        self.items.update_many({"source_id": old_id}, {"$set": {"source_id": new_id}})
        return existing

    def delete_source(self, sid: str) -> dict:
        r = self.items.delete_many({"source_id": sid, "status": {"$in": [R.PENDING, R.SKIPPED, R.FAILED]}})
        self.sources.delete_one({"_id": sid})
        return {"deleted_items": r.deleted_count}

    def known_keys(self, sid: str) -> list[str]:
        return [d["key"] for d in self.items.find({"source_id": sid}, {"key": 1})]

    # ── items ────────────────────────────────────────────────────────────

    def upsert_found(self, source: dict, found: Iterable[dict], status: str) -> int:
        """写入一批检测结果，返回**新插入**的条数。已有的只刷新元数据、不动状态。"""
        n_new = 0
        now = _now()
        platform = source["platform"]
        for it in found:
            key = str(it.get("key") or "")
            if not key:
                continue
            iid = R.item_id(platform, key)
            meta = {
                "title": it.get("title") or "",
                "author": it.get("author") or "",
                "duration": it.get("duration"),
                "cover_url": it.get("cover_url") or "",
                "url": it.get("url") or "",
                "nonce_id": it.get("nonce_id") or "",
            }
            if it.get("published_at"):
                meta["published_at"] = _ts(it["published_at"])
            if it.get("size"):
                meta["size"] = it["size"]
            r = self.items.update_one(
                {"_id": iid},
                {"$set": meta,
                 "$setOnInsert": {"platform": platform, "key": key, "source_id": source["_id"],
                                  "source_name": source.get("name") or "",
                                  "status": status, "attempts": 0, "discovered_at": now}},
                upsert=True,
            )
            if r.upserted_id is not None:
                n_new += 1
        return n_new

    def list_items(self, status: Optional[str] = None, platform: Optional[str] = None,
                   source_id: Optional[str] = None, limit: int = 200, skip: int = 0) -> list[dict]:
        q: dict[str, Any] = {}
        if status:
            q["status"] = status
        if platform:
            q["platform"] = platform
        if source_id:
            q["source_id"] = source_id
        sort = [("done_at", DESCENDING)] if status == R.DONE else [("discovered_at", DESCENDING)]
        return list(self.items.find(q).sort(sort).skip(skip).limit(limit))

    def count_by_status(self) -> dict:
        out = {s: 0 for s in R.ITEM_STATUSES}
        for row in self.items.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
            out[row["_id"]] = row["n"]
        return out

    def count_by_source(self) -> dict:
        out: dict[str, dict] = {}
        for row in self.items.aggregate([
            {"$group": {"_id": {"s": "$source_id", "st": "$status"}, "n": {"$sum": 1}}}]):
            out.setdefault(row["_id"]["s"], {})[row["_id"]["st"]] = row["n"]
        return out

    def pending_items(self, ids: Optional[list[str]] = None) -> list[dict]:
        q: dict[str, Any] = {"status": R.PENDING}
        if ids:
            q["_id"] = {"$in": ids}
        return list(self.items.find(q).sort([("discovered_at", ASCENDING)]))

    def set_item(self, iid: str, fields: dict, inc: Optional[dict] = None) -> None:
        upd: dict[str, Any] = {"$set": fields}
        if inc:
            upd["$inc"] = inc
        self.items.update_one({"_id": iid}, upd)

    def set_status(self, ids: list[str], status: str, only_from: Optional[list[str]] = None) -> int:
        q: dict[str, Any] = {"_id": {"$in": ids}}
        if only_from:
            q["status"] = {"$in": only_from}
        extra = {"error": None} if status == R.PENDING else {}
        return self.items.update_many(q, {"$set": {"status": status, **extra}}).modified_count

    def skip_all(self, status: str) -> int:
        """「清空」待下载 / 失败 = 整批改成已跳过，**不删**。

        删掉的话下次检测会把它们当新条目重新插入成待下载（状态只在插入时写），
        清空等于没清；改成已跳过既稳定，又能在「已跳过」里找回来。
        """
        if status not in (R.PENDING, R.FAILED):
            raise ValueError(status)
        return self.items.update_many({"status": status},
                                      {"$set": {"status": R.SKIPPED}}).modified_count

    def requeue_failed(self) -> int:
        return self.items.update_many({"status": R.FAILED},
                                      {"$set": {"status": R.PENDING, "error": None}}).modified_count

    def reset_downloading(self, reason: str) -> int:
        return self.items.update_many(
            {"status": R.DOWNLOADING},
            {"$set": {"status": R.PENDING, "error": reason}, "$unset": {"progress": ""}}).modified_count

    # ── runs ─────────────────────────────────────────────────────────────

    def start_run(self, kind: str, trigger: str) -> str:
        r = self.runs.insert_one({"kind": kind, "trigger": trigger, "status": "running",
                                  "started_at": _now(), "ended_at": None,
                                  "counts": {}, "log": [], "current": None, "error": None})
        return str(r.inserted_id)

    def update_run(self, run_id: str, fields: dict, log_lines: Optional[list[str]] = None) -> None:
        upd: dict[str, Any] = {"$set": fields}
        if log_lines:
            upd["$push"] = {"log": {"$each": log_lines, "$slice": -200}}
        self.runs.update_one({"_id": ObjectId(run_id)}, upd)

    def finish_run(self, run_id: str, status: str, counts: dict, error: Optional[str] = None) -> None:
        self.runs.update_one({"_id": ObjectId(run_id)}, {"$set": {
            "status": status, "counts": counts, "error": error, "ended_at": _now(), "current": None}})

    def recent_runs(self, limit: int = 15) -> list[dict]:
        return list(self.runs.find({}, {"log": {"$slice": -40}}).sort("started_at", DESCENDING).limit(limit))

    def clear_runs(self) -> int:
        """删掉运行记录，正在跑的那条留着（进度条和「停止」都靠它）。"""
        return self.runs.delete_many({"status": {"$ne": "running"}}).deleted_count

    def running_run(self) -> Optional[dict]:
        return self.runs.find_one({"status": "running"}, sort=[("started_at", DESCENDING)])

    def interrupt_running(self, reason: str) -> int:
        return self.runs.update_many({"status": "running"}, {"$set": {
            "status": "interrupted", "error": reason, "ended_at": _now(), "current": None}}).modified_count


_repo: Optional[MediaRepo] = None


def get_media_repo() -> MediaRepo:
    global _repo
    if _repo is None:
        _repo = MediaRepo()
        _repo.ensure_indexes()
    return _repo
