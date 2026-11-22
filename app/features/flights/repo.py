"""单日查询结果的 Mongo 缓存（集合 flights_cache）。

存的是 Trip 的**原始解析结果**（全部行程，含补查合并后的），不是过滤后的 ——
换一组筛选开关时直接复用，不再请求 Trip。

键 = 出发 + 到达 + 日期 + 返程日（单程为空）+ 舱位 + 下发给 Trip 的「最多中转」。
最后一项不能省：服务端筛选改变了原始结果本身。查的时候「更宽的」结果也能用
（缓存里是最多中转 1 次的，现在只看直飞，照样命中；反过来不行）。

过期靠 TTL 索引（expire_at 字段 + expireAfterSeconds=0）：写入时按当下的
`flights_cache_ttl_minutes` 算好过期时刻，改配置不必重建索引。Mongo 的 TTL 清理
约每 60 秒跑一次，所以读的时候再按 expire_at 过滤一遍。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.clients.mongo import get_collection
from app.core.settings import settings

CABIN = "Y"   # 一期固定经济舱、1 位成人
UNLIMITED = 99  # 「不限中转」在键里的写法（便于用 $gte 比较）


class FlightsCacheRepo:
    def __init__(self):
        self.col = get_collection("flights_cache", settings.mongo_default_db)
        self.col.create_index("expire_at", expireAfterSeconds=0)
        self.col.create_index([("origin", 1), ("dest", 1), ("date", 1), ("return_date", 1), ("cabin", 1)])

    @staticmethod
    def _base(origin: str, dest: str, date: str, return_date: Optional[str]) -> dict:
        return {"origin": origin, "dest": dest, "date": date, "return_date": return_date or "", "cabin": CABIN}

    def get(self, origin: str, dest: str, date: str, return_date: Optional[str],
            transfers: Optional[int]) -> Optional[dict[str, Any]]:
        t = UNLIMITED if transfers is None else transfers
        q = {**self._base(origin, dest, date, return_date),
             "transfers": {"$gte": t}, "expire_at": {"$gt": datetime.now(timezone.utc)}}
        # 有多份可用时取最新的；同样新时取筛得最紧的（条数少，补查截断的可能小）
        return self.col.find_one(q, sort=[("fetched_at", -1), ("transfers", 1)])

    def put(self, origin: str, dest: str, date: str, return_date: Optional[str],
            transfers: Optional[int], payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        key = {**self._base(origin, dest, date, return_date),
               "transfers": UNLIMITED if transfers is None else transfers}
        self.col.replace_one(key, {
            **key, **payload, "fetched_at": now,
            "expire_at": now + timedelta(minutes=settings.flights_cache_ttl_minutes),
        }, upsert=True)


_repo: Optional[FlightsCacheRepo] = None


def repo() -> FlightsCacheRepo:
    global _repo
    if _repo is None:
        _repo = FlightsCacheRepo()
    return _repo
