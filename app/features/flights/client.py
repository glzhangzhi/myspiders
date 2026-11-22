"""Trip.com 机票接口：发请求 + 把响应解析成 dataclass。不含任何业务逻辑（筛选、补查、缓存都在 service）。

非官方接口（网页自己在用的那套），2026-11-22 实测纯 HTTP 可用，不要 cookie / 登录 / 签名。
坑与字段说明见 docs/flights.md。这里最要紧的一条：**任何看不懂的响应都抛 TripError**，
绝不能返回空列表 —— 「接口坏了」被当成「这天没航班」是最难发现的静默失效。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

from app.core.settings import settings

BASE = "https://www.trip.com"
SEARCH_PATH = "/restapi/soa2/27015/FlightListSearchSSE"
POI_PATH = "/restapi/soa2/14427/poiSearch"

# Trip 的 sortInfoType.orderBy：Direct 是网页默认（直飞置顶），Price / Duration 用来补查被截断的部分
ORDERS = ("Direct", "Price", "Duration")


class TripError(Exception):
    """接口失败（HTTP 非 200、retCode 非 SUCCESS、响应解析不了）。一律当「查询失败」，不当「无航班」。"""


@dataclass
class Segment:
    flight_no: str
    dep_airport: str
    arr_airport: str
    dep_time: str          # 当地时间 "YYYY-MM-DD HH:MM:SS"（Trip 原样）
    arr_time: str
    duration: int = 0      # 分钟
    trans_split: bool = False   # True = 自助中转（这一段单独出票）
    different_airport: bool = False  # 与上一段不在同一机场起降（如 SHA 落地、PVG 起飞）


@dataclass
class Itinerary:
    segments: list[Segment]
    duration: int                      # 总时长（分钟）
    price: float                       # 所有 policy 的 totalPrice 最小值
    flags: list[str] = field(default_factory=list)   # journeyFlags：DIRECT / ONE_STOP / LUGGAGE_DIRECT / SHARE_FLIGHT…
    checked_bag: Optional[str] = None  # 最便宜那个 policy 的免费托运额度文案；None = 不含免费托运

    @property
    def key(self) -> str:
        """去重键：航班号组合 + 首段起飞时间（同一天里足够唯一）。"""
        return "+".join(s.flight_no for s in self.segments) + "@" + (self.segments[0].dep_time if self.segments else "")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Itinerary":
        return cls(**{**d, "segments": [Segment(**s) for s in d.get("segments") or []]})


@dataclass
class SearchResult:
    itineraries: list[Itinerary]
    record_count: int                  # basicInfo.recordCount；顶到上限（25）说明被截断
    flags: list[str] = field(default_factory=list)   # basicInfo.flags：HAS_DIRECT_FLIGHT / NO_DIRECT_FLIGHT …
    sub_code: str = ""                 # head.retSubCode：AGG_NO_RESULT / FILTER_NO_RESULT 表示「确实没有」
    skipped: int = 0                   # 解析不了而跳过的行程数（结构变了的前兆）


# ── 请求体 ──────────────────────────────────────────────────────────────


def transfer_value(max_transfers: Optional[int]) -> Optional[str]:
    """服务端「最多中转 N 次」筛选值：0 → "0"，1 → "0|1"，2 → "0|1|2"；None = 不限（不下发）。"""
    if max_transfers is None:
        return None
    return "|".join(str(i) for i in range(max(0, max_transfers) + 1))


def build_body(origin: str, dest: str, date: str, *, return_date: Optional[str] = None,
               order: str = "Direct", max_transfers: Optional[int] = None,
               currency: Optional[str] = None) -> dict:
    cur = currency or settings.flights_currency
    journeys = [{"journeyNo": 1, "departDate": date, "departCode": origin, "arriveCode": dest,
                 "departAirport": "", "arriveAirport": ""}]
    if return_date:
        journeys.append({"journeyNo": 2, "departDate": return_date, "departCode": dest, "arriveCode": origin,
                         "departAirport": "", "arriveAirport": ""})
    tv = transfer_value(max_transfers)
    items = [] if tv is None else [
        {"type": 1, "key": "TRANSFER_COUNT", "value": tv, "journeyNo": j["journeyNo"], "memory": False}
        for j in journeys
    ]
    return {
        "mode": 0,
        "searchCriteria": {
            "grade": 1, "realGrade": 1, "tripType": 2 if return_date else 1, "journeyNo": 1,
            "passengerInfoType": {"adultCount": 1, "childCount": 0, "infantCount": 0},
            "journeyInfoTypes": journeys,
        },
        "sortInfoType": {"direction": True, "orderBy": order, "topList": []},
        "tagList": [],
        "flagList": ["NEED_RESET_SORT", "FullDataCache"],
        "filterType": {"filterFlagTypes": [], "queryItemSettings": items, "studentsSelectedStatus": True},
        "head": {
            "cver": "3", "lang": "01", "sid": "8888", "syscode": "40",
            "extension": [
                {"name": "source", "value": "ONLINE"}, {"name": "sotpGroup", "value": "Trip"},
                {"name": "sotpLocale", "value": "en-XX"}, {"name": "sotpCurrency", "value": cur},
            ],
            "Locale": "en-XX", "Language": "en", "Currency": cur, "appid": "700020",
        },
    }


def headers(currency: Optional[str] = None) -> dict:
    return {
        "content-type": "application/json",
        "currency": currency or settings.flights_currency,
        "locale": "en-XX",
        "user-agent": settings.flights_user_agent,
    }


# ── 解析 ────────────────────────────────────────────────────────────────


def _checked_bag(policy: dict) -> Optional[str]:
    for t in policy.get("tagList") or []:
        if t.get("key") == "FREE_CHECKED_BAGGAGE":
            for e in t.get("extendInfo") or []:
                if e.get("extendKey") == "bagFormat" and e.get("extendValue"):
                    return str(e["extendValue"])
            return str(t.get("content") or "含托运")
    return None


def parse_itinerary(raw: dict) -> Itinerary:
    """一条行程。缺关键字段直接 KeyError/TypeError，由调用方计数。"""
    j = raw["journeyList"][0]
    segs = []
    for s in j["transSectionList"]:
        fi = s.get("flightInfo") or {}
        segs.append(Segment(
            flight_no=str(fi.get("flightNo") or s.get("transportType") or "?"),
            dep_airport=s["departPoint"]["airportCode"],
            arr_airport=s["arrivePoint"]["airportCode"],
            dep_time=s["departDateTime"],
            arr_time=s["arriveDateTime"],
            duration=int(s.get("duration") or 0),
            trans_split=bool(s.get("transSplit")),
            different_airport="DIFFERENT_AIRPORT" in (s.get("sectionFlags") or []),
        ))
    if not segs:
        raise ValueError("行程没有航段")
    policies = [p for p in raw.get("policies") or [] if (p.get("price") or {}).get("totalPrice") is not None]
    if not policies:
        raise ValueError("行程没有价格")
    cheapest = min(policies, key=lambda p: float(p["price"]["totalPrice"]))
    return Itinerary(
        segments=segs,
        duration=int(j.get("duration") or 0),
        price=float(cheapest["price"]["totalPrice"]),
        flags=list(j.get("journeyFlags") or []),
        checked_bag=_checked_bag(cheapest),
    )


def parse_response(text: str) -> SearchResult:
    """`data:` 前缀 + JSON。retCode 非 SUCCESS、解析失败、结构不对都抛 TripError。"""
    body = text.strip()
    if body.startswith("data:"):
        body = body[5:]
    try:
        d = json.loads(body)
    except (ValueError, TypeError) as e:
        raise TripError(f"响应不是 JSON：{text[:120]!r}") from e
    if not isinstance(d, dict):
        raise TripError(f"响应结构不对（顶层是 {type(d).__name__}）")
    head = d.get("head") or {}
    ret = head.get("retCode")
    sub = str(head.get("retSubCode") or "")
    if ret != "SUCCESS":
        raise TripError(f"Trip 返回 {ret or '无 retCode'}{'/' + sub if sub else ''}")
    basic = d.get("basicInfo") or {}
    raw_list = d.get("itineraryList")
    if raw_list is None:
        raw_list = []
    if not isinstance(raw_list, list):
        raise TripError("itineraryList 不是数组")
    its, skipped = [], 0
    for raw in raw_list:
        try:
            its.append(parse_itinerary(raw))
        except (KeyError, TypeError, ValueError, IndexError):
            skipped += 1
    if raw_list and not its:
        raise TripError(f"{len(raw_list)} 条行程全部解析失败（响应结构可能变了）")
    count = int(basic.get("recordCount") or len(raw_list))
    if count > 0 and not raw_list:
        raise TripError(f"recordCount={count} 但没有 itineraryList（响应结构可能变了）")
    return SearchResult(itineraries=its, record_count=count, flags=list(basic.get("flags") or []),
                        sub_code=sub, skipped=skipped)


# ── 请求 ────────────────────────────────────────────────────────────────


async def search(http: httpx.AsyncClient, origin: str, dest: str, date: str, *,
                 return_date: Optional[str] = None, order: str = "Direct",
                 max_transfers: Optional[int] = None) -> SearchResult:
    """发一次 FlightListSearchSSE。限速与串行由调用方（service）负责。"""
    try:
        r = await http.post(BASE + SEARCH_PATH, headers=headers(),
                            json=build_body(origin, dest, date, return_date=return_date, order=order,
                                            max_transfers=max_transfers),
                            timeout=settings.flights_timeout_seconds)
    except httpx.HTTPError as e:
        raise TripError(f"网络错误：{type(e).__name__} {e}") from e
    if r.status_code != 200:
        hint = "（被 whaleguard 拦截，检查 UA 或请求体）" if r.status_code == 432 else ""
        raise TripError(f"HTTP {r.status_code}{hint}")
    return parse_response(r.text)


async def poi_search(http: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
    """城市/机场自动补全（Trip 搜索框用的 poiSearch）。返回 [{code, name, city, country, kind}]。"""
    r = await http.post(BASE + POI_PATH, headers=headers(), timeout=10, json={
        "key": key, "mode": "0", "tripType": "OW",
        "Head": {"Group": "Trip", "Source": "ONLINE", "Version": "3",
                 "Currency": settings.flights_currency, "Locale": "en-XX"},
    })
    if r.status_code != 200:
        raise TripError(f"HTTP {r.status_code}")
    return parse_poi(r.json())


def parse_poi(d: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for x in (d or {}).get("results") or []:
        code = x.get("airportCode") or x.get("cityCode")
        if code and x.get("isCanSelect", 1):
            out.append({"code": code, "name": x.get("name") or code, "country": x.get("countryName") or "",
                        "kind": "airport" if x.get("airportCode") else "city"})
        for c in x.get("childResults") or []:
            if c.get("airportCode"):
                out.append({"code": c["airportCode"], "name": c.get("name") or c["airportCode"],
                            "country": c.get("countryName") or "", "kind": "airport"})
    seen, uniq = set(), []
    for o in out:
        k = (o["code"], o["kind"])
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq[:12]
