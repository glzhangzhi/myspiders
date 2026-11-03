"""Joybuy 降价监控的状态与价格历史。

两个集合：
  - joybuy_state    单文档(key="singleton")：cookie、推送节流、最近一次结果
  - joybuy_prices   每次抓到的价格一条，用于画走势、也用于事后复盘「什么时候降的」

**cookie 放 Mongo 不放 .env**：改 .env 要重启服务，而 `mfs_session` 会随登录态
失效，重灌是常规操作、不该每次都重启。短命凭证一律照此办理。
值本身是**凭证**，故 `snapshot()` 绝不回显原文，只回指纹与长度。

价格历史**只在价格变化时写一条**（不是每小时一条）。每小时一条在一年后是 8760 条
全是重复值的记录，而「什么时候变的」才是唯一有价值的信息；想确认爬虫活着看
`last_checked_at`，不靠堆记录。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.clients.mongo import get_collection
from app.core.settings import settings

_KEY = "singleton"


def _fingerprint(text: str) -> str:
    """短哈希做标识 —— 绝不把 cookie 原文写进日志或快照。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class JoybuyWatchRepository:
    def __init__(self):
        self.state = get_collection("joybuy_state", settings.mongo_default_db)
        self.state.create_index("key", unique=True)
        self.prices = get_collection("joybuy_prices", settings.mongo_default_db)
        self.prices.create_index([("sku", 1), ("checked_at", -1)])

    # ------------------------------------------------------------------ 状态
    def _doc(self) -> Dict[str, Any]:
        return self.state.find_one({"key": _KEY}) or {}

    def _set(self, fields: Dict[str, Any]) -> None:
        self.state.update_one(
            {"key": _KEY},
            {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    # ----------------------------------------------------------------- cookie
    def get_cookie(self) -> Optional[str]:
        return self._doc().get("cookie") or None

    def set_cookie(self, cookie: str) -> None:
        """写入新 cookie，并重置与旧 cookie 绑定的失效告警节流。"""
        cookie = cookie.strip()
        self._set(
            {
                "cookie": cookie,
                "cookie_fingerprint": _fingerprint(cookie),
                "cookie_set_at": datetime.now(timezone.utc),
                "blocked_warned_for": None,  # 新凭证 → 允许再次告警
            }
        )

    def should_warn_blocked(self) -> bool:
        """同一份 cookie 的「失效了」只推一次 —— 否则每小时一条，一夜 8 条。"""
        cookie = self.get_cookie() or ""
        fp = _fingerprint(cookie)
        if self._doc().get("blocked_warned_for") == fp:
            return False
        self._set({"blocked_warned_for": fp})
        return True

    # ------------------------------------------------------- 本次抓取结果留痕
    def record_check(
        self,
        sku: str,
        price: Optional[float],
        *,
        ok: bool,
        detail: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """记录一次检查，返回**本次之前**的最新价格（供调用方判断是否变化）。

        返回上一次价格而不是让调用方自己查：两步之间若有并发写入就会读到
        被自己刚写进去的值，于是「变化」永远判定不出来。
        """
        prev = self.latest_price(sku)
        fields: Dict[str, Any] = {
            "last_checked_at": datetime.now(timezone.utc),
            "last_ok": ok,
            "last_detail": detail,
        }
        if ok and price is not None:
            fields["last_price"] = price
        if extra:
            fields.update({f"last_{k}": v for k, v in extra.items()})
        self._set(fields)

        # 只在价格真的变了（或首次）时追加历史
        if ok and price is not None and (prev is None or abs(prev - price) > 1e-9):
            doc = {
                "sku": sku,
                "price": price,
                "checked_at": datetime.now(timezone.utc),
            }
            if extra:
                doc.update(extra)
            self.prices.insert_one(doc)
        return prev

    def latest_price(self, sku: str) -> Optional[float]:
        doc = self.prices.find_one({"sku": sku}, sort=[("checked_at", -1)])
        return float(doc["price"]) if doc and doc.get("price") is not None else None

    def history(self, sku: str, limit: int = 200) -> List[Dict[str, Any]]:
        out = []
        for d in self.prices.find({"sku": sku}).sort("checked_at", -1).limit(min(limit, 1000)):
            d.pop("_id", None)
            out.append(d)
        return out

    # ------------------------------------------------------------ 推送节流
    def should_notify_deal(self, sku: str, price: float) -> bool:
        """低价推送的节流：同一个 sku，**只有在价格比上次推送时更低**才再推。

        不是「推过就永不再推」—— 从 €9.99 掉到 €4.99 值得再响一次；
        也不是「每次低于阈值都推」—— 那会在促销持续的几天里每小时响一遍，
        而那正是训练人忽略整个 topic 的做法。
        """
        key = f"notified_price_{sku}"
        last = self._doc().get(key)
        if last is not None and price >= float(last) - 1e-9:
            return False
        self._set({key: price, f"notified_at_{sku}": datetime.now(timezone.utc)})
        return True

    def reset_notify(self, sku: str) -> None:
        """清掉推送节流（改了阈值或想重新收到提醒时用）。"""
        self.state.update_one(
            {"key": _KEY},
            {"$unset": {f"notified_price_{sku}": "", f"notified_at_{sku}": ""}},
        )

    # ------------------------------------------------------------- 阈值配置
    def get_threshold(self) -> float:
        val = self._doc().get("threshold")
        try:
            val = float(val)
        except (TypeError, ValueError):
            return float(settings.joybuy_price_threshold)
        return val if val > 0 else float(settings.joybuy_price_threshold)

    def set_threshold(self, value: float) -> None:
        self._set({"threshold": float(value)})

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> Dict[str, Any]:
        """给仪表盘看的安全快照 —— 绝不含 cookie 原文。"""
        doc = self._doc()
        doc.pop("_id", None)
        cookie = doc.pop("cookie", None)
        doc.update(
            {
                "has_cookie": bool(cookie),
                "cookie_length": len(cookie) if cookie else 0,
                "threshold": self.get_threshold(),
            }
        )
        return doc


_repo: JoybuyWatchRepository | None = None


def _get_repo() -> JoybuyWatchRepository:
    global _repo
    if _repo is None:
        _repo = JoybuyWatchRepository()
    return _repo


def get_cookie() -> Optional[str]:
    return _get_repo().get_cookie()


def set_cookie(cookie: str) -> None:
    _get_repo().set_cookie(cookie)


def should_warn_blocked() -> bool:
    return _get_repo().should_warn_blocked()


def record_check(sku: str, price: Optional[float], **kw) -> Optional[float]:
    return _get_repo().record_check(sku, price, **kw)


def latest_price(sku: str) -> Optional[float]:
    return _get_repo().latest_price(sku)


def history(sku: str, limit: int = 200) -> List[Dict[str, Any]]:
    return _get_repo().history(sku, limit)


def should_notify_deal(sku: str, price: float) -> bool:
    return _get_repo().should_notify_deal(sku, price)


def reset_notify(sku: str) -> None:
    _get_repo().reset_notify(sku)


def get_threshold() -> float:
    return _get_repo().get_threshold()


def set_threshold(value: float) -> None:
    _get_repo().set_threshold(value)


def snapshot() -> Dict[str, Any]:
    return _get_repo().snapshot()
