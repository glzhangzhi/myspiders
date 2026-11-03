"""任天堂商城价格仓储。"""

from __future__ import annotations

from typing import Dict, Iterable

from pymongo import UpdateOne

from app.clients.mongo import get_collection
from app.core.settings import settings
from app.features.nintendo.models import NinPriceRecord


class NinPriceRepository:
    """封装对 nin_price_records 集合的访问。"""

    def __init__(self):
        self.collection = get_collection("nin_price_records", settings.mongo_default_db)

    def fetch_all(self) -> Dict[str, NinPriceRecord]:
        """读取所有记录并转成 pid -> record 的映射，便于业务层处理。"""

        records: Dict[str, NinPriceRecord] = {}
        for doc in self.collection.find({}):
            doc.pop("_id", None)
            record = NinPriceRecord(**doc)
            records[record.pid] = record
        return records

    def bulk_upsert(self, records: Iterable[NinPriceRecord]):
        """批量 upsert，减少数据库交互次数。"""

        data = list(records)
        if not data:
            return
        operations = [
            UpdateOne(
                {"pid": record.pid},
                {"$set": record.model_dump()},
                upsert=True,
            )
            for record in data
        ]
        self.collection.bulk_write(operations, ordered=False)
