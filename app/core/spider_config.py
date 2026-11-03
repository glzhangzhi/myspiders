"""爬虫配置持久化。"""

from __future__ import annotations

from typing import Dict, Optional

from app.clients.mongo import get_collection
from app.core.settings import settings


class SpiderConfigRepository:
    """管理 spider_configs 集合，只存 spider_id + enabled。"""

    def __init__(self):
        self.collection = get_collection("spider_configs", settings.mongo_default_db)

    def get_enabled_map(self) -> Dict[str, bool]:
        """返回所有已存储配置的 {spider_id: enabled} 映射。"""
        result: Dict[str, bool] = {}
        for doc in self.collection.find({}, {"_id": 0, "spider_id": 1, "enabled": 1}):
            result[doc["spider_id"]] = doc.get("enabled", True)
        return result

    def set_enabled(self, spider_id: str, enabled: bool) -> None:
        """设置某个爬虫的启用状态。"""
        self.collection.update_one(
            {"spider_id": spider_id},
            {"$set": {"enabled": enabled}},
            upsert=True,
        )

    def is_enabled(self, spider_id: str) -> Optional[bool]:
        """查询单个爬虫的启用状态，未存储则返回 None。"""
        doc = self.collection.find_one({"spider_id": spider_id}, {"_id": 0, "enabled": 1})
        if doc is None:
            return None
        return doc.get("enabled", True)


spider_config_repo = SpiderConfigRepository()
