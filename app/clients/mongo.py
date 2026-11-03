"""MongoDB 客户端封装。"""

from functools import lru_cache
from typing import Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from app.core.settings import settings


@lru_cache
def get_mongo_client() -> MongoClient:
    """返回缓存的 MongoClient 实例。"""

    # tz_aware=True：让读出的 datetime 一律带 UTC 时区（aware），应用层才能
    # 安全地 .astimezone() 到目标时区。约定：存储层统一 UTC，展示层各自转换。
    return MongoClient(settings.mongo_uri, tz_aware=True)


def get_database(name: Optional[str] = None) -> Database:
    """获取指定名称的数据库，默认使用 settings.mongo_default_db。"""

    db_name = name or settings.mongo_default_db
    return get_mongo_client()[db_name]


def get_collection(collection_name: str, db_name: Optional[str] = None) -> Collection:
    """获取集合句柄，供仓储层复用。"""

    return get_database(db_name)[collection_name]
