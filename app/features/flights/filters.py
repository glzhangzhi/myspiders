"""筛选与排序的硬规则（纯函数，tests/test_flights.py 钉住）。

DeepSeek 只写点评，**不参与**筛选和排序。被筛掉的行程不丢，带着逐条原因交给前端折叠显示，
让机主知道「是条件排除的」而不是「漏查了」。

行李直挂三态（机主确认「推断直挂」算符合条件，页面标出「推断」）：
  - confirmed  确认直挂：带 LUGGAGE_DIRECT 标记，或直飞
  - inferred   推断直挂：没标记，但所有航段 transSplit 都是 false（同一张票）
  - recheck    需重新托运：任一航段 transSplit 为 true（自助中转、分开出票）
LUGGAGE_DIRECT 时有时无（实测同一天几分钟内 8 条 → 0 条），所以「没标记」≠「不能直挂」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.features.flights.client import Itinerary

CONFIRMED, INFERRED, RECHECK = "confirmed", "inferred", "recheck"


@dataclass
class Filters:
    direct_only: bool = False
    max_transfers: Optional[int] = 1          # None = 不限
    allow_long_layover: bool = False
    long_layover_hours: float = 6.0           # 关闭「接受长时间中转」时，任一次中转超过它就排除
    allow_recheck: bool = False
    recheck_min_layover_hours: float = 3.0    # 接受需重新托运时，每次中转至少这么久
    max_duration_hours: Optional[float] = None

    def server_transfers(self) -> Optional[int]:
        """下发给 Trip 的「最多中转」：只看直飞时就是 0。"""
        return 0 if self.direct_only else self.max_transfers


def fmt_minutes(m: int) -> str:
    h, mm = divmod(int(m), 60)
    return f"{h}h{mm:02d}" if mm else f"{h}h"


def _hours(h: float) -> str:
    return f"{h:g}h"


def _dt(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def layovers(it: Itinerary) -> list[dict]:
    """每次中转：{airport, minutes, change_airport}。时长 = 后段起飞 − 前段到达（同城当地时间，可直接相减）。"""
    out = []
    segs = it.segments
    for a, b in zip(segs, segs[1:]):
        mins = int((_dt(b.dep_time) - _dt(a.arr_time)).total_seconds() // 60)
        airport = a.arr_airport if a.arr_airport == b.dep_airport else f"{a.arr_airport}→{b.dep_airport}"
        out.append({"airport": airport, "minutes": mins,
                    "change_airport": a.arr_airport != b.dep_airport or b.different_airport})
    return out


def baggage_status(it: Itinerary) -> str:
    if len(it.segments) <= 1 or "LUGGAGE_DIRECT" in it.flags:
        return CONFIRMED
    if any(s.trans_split for s in it.segments):
        return RECHECK
    return INFERRED


def reject_reasons(it: Itinerary, f: Filters) -> list[str]:
    """不符合条件的原因（空列表 = 符合）。"""
    reasons: list[str] = []
    n = len(it.segments) - 1
    if f.direct_only and n > 0:
        reasons.append(f"非直飞（中转 {n} 次）")
    elif f.max_transfers is not None and n > f.max_transfers:
        reasons.append(f"中转 {n} 次，超过 {f.max_transfers} 次")
    lays = layovers(it)
    if not f.allow_long_layover:
        limit = f.long_layover_hours * 60
        for l in lays:
            if l["minutes"] > limit:
                reasons.append(f"在 {l['airport']} 中转 {fmt_minutes(l['minutes'])}，超过 {_hours(f.long_layover_hours)}")
    if baggage_status(it) == RECHECK:
        if not f.allow_recheck:
            reasons.append("自助中转，需重新托运")
        else:
            need = f.recheck_min_layover_hours * 60
            for l in lays:
                if l["minutes"] < need:
                    reasons.append(f"自助中转需重新托运，在 {l['airport']} 只有 {fmt_minutes(l['minutes'])}，"
                                   f"不足 {_hours(f.recheck_min_layover_hours)}")
    if f.max_duration_hours is not None and it.duration > f.max_duration_hours * 60:
        reasons.append(f"总时长 {fmt_minutes(it.duration)}，超过 {_hours(f.max_duration_hours)}")
    return reasons


def describe(it: Itinerary) -> dict:
    """给前端和点评用的一条行程摘要（不含筛选结论）。"""
    lays = layovers(it)
    return {
        "key": it.key,
        "flights": [s.flight_no for s in it.segments],
        "price": it.price,
        "duration": it.duration,
        "dep_time": it.segments[0].dep_time,
        "arr_time": it.segments[-1].arr_time,
        "from": it.segments[0].dep_airport,
        "to": it.segments[-1].arr_airport,
        "stops": len(it.segments) - 1,
        "layovers": lays,
        "baggage": baggage_status(it),
        "checked_bag": it.checked_bag,
        "codeshare": "SHARE_FLIGHT" in it.flags or "ALL_SHARE_FLIGHT" in it.flags,
        "segments": [
            {"flight_no": s.flight_no, "from": s.dep_airport, "to": s.arr_airport,
             "dep": s.dep_time, "arr": s.arr_time, "self_transfer": s.trans_split}
            for s in it.segments
        ],
    }


def sort_key(d: dict) -> tuple:
    return (d["price"], d["duration"], d["dep_time"])


def apply(its: list[Itinerary], f: Filters) -> tuple[list[dict], list[dict]]:
    """→ (符合条件的, 被排除的)，各自按价格、再按总时长升序；被排除的带 reasons。"""
    ok, bad = [], []
    for it in its:
        d = describe(it)
        r = reject_reasons(it, f)
        if r:
            d["reasons"] = r
            bad.append(d)
        else:
            ok.append(d)
    ok.sort(key=sort_key)
    bad.sort(key=sort_key)
    return ok, bad


def merge_unique(*lists: list[Itinerary]) -> list[Itinerary]:
    """多次补查的结果按「航班号组合 + 起飞时间」去重合并；同一条取较低价（价格会浮动几十欧）。"""
    best: dict[str, Itinerary] = {}
    for lst in lists:
        for it in lst:
            cur = best.get(it.key)
            if cur is None or it.price < cur.price:
                best[it.key] = it
    return list(best.values())
