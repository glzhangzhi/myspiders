"""Claude 用量历史 + 告警状态的 Mongo 持久化。

两个集合（均在 mongo_default_db）：
  - claude_usage_history: 每次轮询追加一条原始快照，用于还原消耗时间序列
  - claude_usage_state:   单文档，保存每个窗口的告警/重置状态（峰值、已通知阈值等）
"""

from __future__ import annotations

from app.clients.mongo import get_collection
from app.core.logging import get_logger
from app.core.settings import settings

logger = get_logger("CLAUDE_USAGE_REPO")


class ClaudeUsageRepository:
    def __init__(self) -> None:
        self.history = get_collection("claude_usage_history", settings.mongo_default_db)
        self.state = get_collection("claude_usage_state", settings.mongo_default_db)
        try:
            self.history.create_index("ts")
        except Exception:
            # Mongo 暂时不可用也不应阻断应用启动；索引下次有机会再建
            logger.warning("创建 claude_usage_history.ts 索引失败（忽略）")

    # ── 历史样本 ──

    def insert_sample(self, sample: dict) -> None:
        # 传入副本，避免 pymongo 往调用方的 dict 注入 _id
        self.history.insert_one(dict(sample))

    def samples_since(self, since_ts: float) -> list[dict]:
        return list(
            self.history.find({"ts": {"$gte": since_ts}}, {"_id": 0}).sort("ts", 1)
        )

    # ── 告警/重置状态（单文档）──

    def get_state(self) -> dict:
        doc = self.state.find_one({"_id": "windows"}, {"_id": 0})
        return doc or {}

    def save_state(self, state: dict) -> None:
        self.state.update_one({"_id": "windows"}, {"$set": state}, upsert=True)


claude_usage_repo = ClaudeUsageRepository()
