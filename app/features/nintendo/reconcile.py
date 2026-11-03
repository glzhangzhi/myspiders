"""把一次抓取结果并进已有记录（纯函数，tests/test_nintendo_reconcile.py 守着）。

数据源是 eShop 的「特价」列表页，只列在打折的游戏。所以：

- 列表里有、库里没有 → 新折扣
- 列表里有、库里标着已结束 → 重新打折（对人来说和新折扣一样值得知道）
- 列表里有、价格变了 → 变价；只有更便宜才算值得推送的事件，涨价（折扣变浅）静默更新
  —— 原先涨价直接被忽略，库里一直留着更低的旧价，页面显示的价格买不到
- 库里在打折、列表里没有 → 折扣结束

最后一条有前提：**这次抓取必须完整**。翻页超时、选择器失效都会让列表少一截，此时把
缺的那部分当成「折扣结束」会一次性错杀几百条，而且不报错。所以调用方只在确认完整时
传 listed（本轮列表里出现过的全部 pid，含价格解析失败的那些）；传 None 就不做结束判定。

即便完整，翻页期间列表排序变动也可能让个别游戏两页都没赶上、被误判为结束。所以
「结束不到 3 天、又以同一价格回来」视为误判，静默恢复，不当成重新打折去推送。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from app.features.nintendo.models import NinPriceRecord

FLAP_WINDOW = timedelta(days=3)


@dataclass(frozen=True)
class Scraped:
    pid: str
    title: str
    original_price: float
    current_price: float

    @property
    def discount_percent(self) -> float:
        if self.original_price <= 0:
            return 0.0
        return round((self.original_price - self.current_price) / self.original_price * 100, 2)


@dataclass(frozen=True)
class Event:
    kind: str          # new | resale | deeper
    record: NinPriceRecord


def reconcile(
    existing: Dict[str, NinPriceRecord],
    scraped: List[Scraped],
    now: datetime,
    listed: Optional[Set[str]],
) -> Tuple[List[NinPriceRecord], List[Event]]:
    """返回 (需要写回的记录, 值得推送的事件)。不修改 existing。"""
    changed: Dict[str, NinPriceRecord] = {}
    events: List[Event] = []
    seen: Set[str] = set()

    for s in scraped:
        if s.pid in seen:          # 同一游戏在相邻两页各出现一次（翻页期间排序变动）
            continue
        seen.add(s.pid)
        old = existing.get(s.pid)
        price = dict(
            title=s.title,
            original_price=s.original_price,
            current_price=s.current_price,
            discount_percent=s.discount_percent,
        )
        if old is None:
            rec = NinPriceRecord(pid=s.pid, **price, updated_at=now, on_sale=True,
                                 sale_started_at=now, last_seen_at=now)
            changed[s.pid] = rec
            events.append(Event("new", rec))
        elif not old.on_sale:
            flap = (old.sale_ended_at is not None and now - old.sale_ended_at < FLAP_WINDOW
                    and s.current_price == old.current_price)
            if flap:
                changed[s.pid] = old.model_copy(update=dict(
                    title=s.title, on_sale=True, sale_ended_at=None, last_seen_at=now))
            else:
                rec = old.model_copy(update=dict(**price, updated_at=now, on_sale=True,
                                                 sale_started_at=now, sale_ended_at=None,
                                                 last_seen_at=now))
                changed[s.pid] = rec
                events.append(Event("resale", rec))
        elif s.current_price != old.current_price or s.original_price != old.original_price:
            rec = old.model_copy(update=dict(**price, updated_at=now, last_seen_at=now))
            changed[s.pid] = rec
            if s.current_price < old.current_price:
                events.append(Event("deeper", rec))
        else:
            # 没变化也要写 last_seen_at：它是「这条还在打折」的唯一证据
            changed[s.pid] = old.model_copy(update=dict(title=s.title, last_seen_at=now))

    if listed is not None:
        for pid, old in existing.items():
            if old.on_sale and pid not in seen and pid not in listed:
                changed[pid] = old.model_copy(update=dict(on_sale=False, sale_ended_at=now))

    return list(changed.values()), events
