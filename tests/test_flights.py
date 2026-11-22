"""机票面板的纯函数测试：解析、行李三态、中转时长、筛选原因、25 条补查合并、矩阵组装、失败≠无航班。

fixture 是 2026-11-22 抓的 TPE→FRA 2026-12-26 真实响应，删掉了 ResponseStatus / token / uniqueId
等标识，只留 6 条代表性行程（直飞、上海中转、香港中转、阿布扎比中转、两次中转）。

    .venv/bin/pytest tests/test_flights.py -v
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.features.flights import client, filters as flt, service
from app.features.flights.client import Itinerary, Segment, TripError

FIX = Path(__file__).parent / "fixtures" / "flights" / "tpe_fra_20261226.txt"


@pytest.fixture(scope="module")
def raw_text() -> str:
    return FIX.read_text()


@pytest.fixture(scope="module")
def result(raw_text):
    return client.parse_response(raw_text)


def _by(result, flights: str) -> Itinerary:
    return next(it for it in result.itineraries if "+".join(s.flight_no for s in it.segments) == flights)


def _it(*legs, flags=(), price=500.0, duration=None) -> Itinerary:
    """legs: (flight, from, to, dep, arr[, trans_split])"""
    segs = [Segment(flight_no=l[0], dep_airport=l[1], arr_airport=l[2], dep_time=l[3], arr_time=l[4],
                    trans_split=bool(l[5]) if len(l) > 5 else False) for l in legs]
    return Itinerary(segments=segs, duration=duration or 900, price=price, flags=list(flags))


# ── 解析 ────────────────────────────────────────────────────────────────


def test_parse_fixture(result):
    assert len(result.itineraries) == 6 and result.skipped == 0
    ci = _by(result, "CI061")
    assert len(ci.segments) == 1
    s = ci.segments[0]
    assert (s.dep_airport, s.arr_airport) == ("TPE", "FRA")
    assert s.dep_time == "2026-12-26 23:30:00" and s.arr_time == "2026-12-27 06:30:00"
    assert ci.duration == 14 * 60
    assert "DIRECT" in ci.flags
    assert ci.price > 0 and ci.checked_bag and "kg" in ci.checked_bag


def test_parse_price_is_cheapest_policy(raw_text):
    d = json.loads(raw_text[5:])
    raw = next(x for x in d["itineraryList"] if x["journeyList"][0]["transSectionList"][0]["flightInfo"]["flightNo"] == "CI061")
    expect = min(p["price"]["totalPrice"] for p in raw["policies"])
    assert client.parse_itinerary(raw).price == expect


def test_parse_failure_is_error_not_empty():
    # 「接口坏了」绝不能变成「这天没航班」
    with pytest.raises(TripError):
        client.parse_response("<html>blocked</html>")
    with pytest.raises(TripError, match="AGG_SEARCH_FAIL"):
        client.parse_response('data:{"head":{"retCode":"FAILURE","retSubCode":"AGG_SEARCH_FAIL"}}')
    with pytest.raises(TripError):
        client.parse_response('data:{"head":{"retCode":"SUCCESS"},"basicInfo":{"recordCount":12}}')
    with pytest.raises(TripError, match="全部解析失败"):
        client.parse_response('data:{"head":{"retCode":"SUCCESS"},"basicInfo":{"recordCount":1},'
                              '"itineraryList":[{"journeyList":[{}]}]}')


@pytest.mark.parametrize("sub", ["AGG_NO_RESULT", "FILTER_NO_RESULT"])
def test_no_result_is_empty(sub):
    r = client.parse_response(f'data:{{"head":{{"retCode":"SUCCESS","retSubCode":"{sub}"}},"basicInfo":{{"recordCount":0}}}}')
    assert r.itineraries == [] and r.sub_code == sub


def test_build_body_transfer_filter():
    b = client.build_body("TPE", "FRA", "2026-12-26", max_transfers=1)
    items = b["filterType"]["queryItemSettings"]
    assert items == [{"type": 1, "key": "TRANSFER_COUNT", "value": "0|1", "journeyNo": 1, "memory": False}]
    assert b["searchCriteria"]["tripType"] == 1
    rt = client.build_body("TPE", "FRA", "2026-12-26", return_date="2027-01-10", max_transfers=0)
    assert rt["searchCriteria"]["tripType"] == 2
    assert [j["departCode"] for j in rt["searchCriteria"]["journeyInfoTypes"]] == ["TPE", "FRA"]
    assert [i["value"] for i in rt["filterType"]["queryItemSettings"]] == ["0", "0"]
    assert client.build_body("TPE", "FRA", "2026-12-26")["filterType"]["queryItemSettings"] == []


def test_parse_poi():
    items = client.parse_poi({"results": [
        {"name": "Shanghai", "cityCode": "SHA", "countryName": "China", "dataType": 3,
         "childResults": [{"name": "Pudong", "airportCode": "PVG"}, {"name": "Hongqiao", "airportCode": "SHA"}]},
    ]})
    assert [(i["code"], i["kind"]) for i in items] == [("SHA", "city"), ("PVG", "airport"), ("SHA", "airport")]


# ── 中转时长与行李三态 ──────────────────────────────────────────────────


def test_layover_from_fixture(result):
    it = _by(result, "CX479+CX289")
    [l] = flt.layovers(it)
    assert l["airport"] == "HKG" and l["minutes"] == 65 and not l["change_airport"]
    multi = _by(result, "VJ941+EY431+EY1046")
    assert [l["minutes"] for l in flt.layovers(multi)] == [360, 80]


def test_layover_across_midnight_and_airport_change():
    it = _it(("MU5006", "TSA", "SHA", "2026-12-26 07:00:00", "2026-12-26 08:45:00"),
             ("MU219", "PVG", "FRA", "2026-12-26 23:55:00", "2026-12-27 06:00:00"))
    [l] = flt.layovers(it)
    assert l["minutes"] == 15 * 60 + 10 and l["airport"] == "SHA→PVG" and l["change_airport"]


def test_baggage_tristate(result):
    assert flt.baggage_status(_by(result, "CI061")) == flt.CONFIRMED          # 直飞
    assert flt.baggage_status(_by(result, "CX479+CX289")) == flt.INFERRED     # 无标记、同一张票
    tagged = _it(("A1", "X", "Y", "2026-12-26 08:00:00", "2026-12-26 10:00:00"),
                 ("A2", "Y", "Z", "2026-12-26 12:00:00", "2026-12-26 20:00:00"), flags=["ONE_STOP", "LUGGAGE_DIRECT"])
    assert flt.baggage_status(tagged) == flt.CONFIRMED
    split = _it(("A1", "X", "Y", "2026-12-26 08:00:00", "2026-12-26 10:00:00"),
                ("B2", "Y", "Z", "2026-12-26 12:00:00", "2026-12-26 20:00:00", True))
    assert flt.baggage_status(split) == flt.RECHECK


def test_trans_split_parsed(raw_text):
    d = json.loads(raw_text[5:])
    raw = copy.deepcopy(d["itineraryList"][1])
    raw["journeyList"][0]["transSectionList"][1]["transSplit"] = True
    assert flt.baggage_status(client.parse_itinerary(raw)) == flt.RECHECK


# ── 筛选 ────────────────────────────────────────────────────────────────


def _split(layover_h: float) -> Itinerary:
    arr = "2026-12-26 10:00:00"
    dep_h = 10 + layover_h
    dep = f"2026-12-26 {int(dep_h):02d}:{int((dep_h % 1) * 60):02d}:00"
    return _it(("A1", "X", "Y", "2026-12-26 08:00:00", arr), ("B2", "Y", "Z", dep, "2026-12-26 23:00:00", True))


def test_recheck_rules():
    f = flt.Filters()
    assert flt.reject_reasons(_split(4), f) == ["自助中转，需重新托运"]
    f.allow_recheck = True
    assert flt.reject_reasons(_split(4), f) == []
    [r] = flt.reject_reasons(_split(2), f)
    assert "只有 2h" in r and "不足 3h" in r


def test_long_layover_reason():
    it = _it(("A1", "X", "Y", "2026-12-26 08:00:00", "2026-12-26 10:00:00"),
             ("A2", "Y", "Z", "2026-12-26 17:20:00", "2026-12-26 23:00:00"))
    assert flt.reject_reasons(it, flt.Filters()) == ["在 Y 中转 7h20，超过 6h"]
    assert flt.reject_reasons(it, flt.Filters(allow_long_layover=True)) == []
    assert flt.reject_reasons(it, flt.Filters(allow_long_layover=True, long_layover_hours=8)) == []


def test_transfer_count_and_duration(result):
    multi = _by(result, "VJ941+EY431+EY1046")
    assert "中转 2 次，超过 1 次" in flt.reject_reasons(multi, flt.Filters())
    assert flt.reject_reasons(_by(result, "CX479+CX289"), flt.Filters(direct_only=True)) == ["非直飞（中转 1 次）"]
    ci = _by(result, "CI061")
    assert flt.reject_reasons(ci, flt.Filters(max_duration_hours=13)) == ["总时长 14h，超过 13h"]
    assert flt.reject_reasons(ci, flt.Filters(max_duration_hours=14)) == []


def test_apply_sorts_and_keeps_rejected(result):
    ok, bad = flt.apply(result.itineraries, flt.Filters())
    assert [d["price"] for d in ok] == sorted(d["price"] for d in ok)
    assert {"+".join(d["flights"]) for d in bad} >= {"MU5006+MU257", "VJ941+EY431+EY1046"}  # 15h35 中转、两次中转
    assert all(d["reasons"] for d in bad)
    assert "CI061" in {"+".join(d["flights"]) for d in ok}


def test_server_transfers():
    assert flt.Filters(direct_only=True, max_transfers=2).server_transfers() == 0
    assert flt.Filters(max_transfers=None).server_transfers() is None


# ── 25 条补查 ───────────────────────────────────────────────────────────


def test_needs_refill():
    assert service.needs_refill(25, 25)
    assert service.needs_refill(25, 24)
    assert not service.needs_refill(23, 23)


def test_merge_unique_keeps_lower_price():
    a = _it(("CI061", "TPE", "FRA", "2026-12-26 23:30:00", "2026-12-27 06:30:00"), price=520)
    b = _it(("CI061", "TPE", "FRA", "2026-12-26 23:30:00", "2026-12-27 06:30:00"), price=503)
    c = _it(("CX479", "TPE", "HKG", "2026-12-26 21:00:00", "2026-12-26 23:00:00"),
            ("CX289", "HKG", "FRA", "2026-12-27 00:05:00", "2026-12-27 06:00:00"))
    merged = flt.merge_unique([a, c], [b])
    assert len(merged) == 2 and min(m.price for m in merged if m.segments[0].flight_no == "CI061") == 503


class _FakeRepo:
    def __init__(self):
        self.store = {}

    def get(self, *a):
        return None

    def put(self, *a):
        self.store[a[:4]] = a[-1]


async def test_fetch_day_refills_on_cap(monkeypatch, raw_text):
    """首查顶到上限 → 按 Price、Duration 各补查一次并去重合并；请求数如实计数。"""
    monkeypatch.setattr(service, "repo", lambda: _FakeRepo())
    monkeypatch.setattr(service.settings, "flights_page_cap", 6)
    monkeypatch.setattr(service.settings, "flights_request_interval_min", 0)
    monkeypatch.setattr(service.settings, "flights_request_interval_max", 0)
    d = json.loads(raw_text[5:])
    extra = copy.deepcopy(d["itineraryList"][0])
    extra["journeyList"][0]["transSectionList"][0]["flightInfo"]["flightNo"] = "XX001"
    orders = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        orders.append(body["sortInfoType"]["orderBy"])
        out = copy.deepcopy(d)
        if orders[-1] == "Price":
            out["itineraryList"] = out["itineraryList"][:5] + [extra]
        return httpx.Response(200, text="data:" + json.dumps(out))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        raw = await service.fetch_day(http, "TPE", "FRA", "2026-12-26", transfers=1)
    assert orders == ["Direct", "Price", "Duration"]
    assert raw.requests == 3 and raw.refilled
    assert len(raw.itineraries) == 7   # 6 + XX001，重复的去掉


async def test_fetch_day_http_error_raises(monkeypatch):
    monkeypatch.setattr(service, "repo", lambda: _FakeRepo())
    monkeypatch.setattr(service.settings, "flights_request_interval_min", 0)
    monkeypatch.setattr(service.settings, "flights_request_interval_max", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(432, content=b"\x00\x01"))) as http:
        with pytest.raises(TripError, match="432"):
            await service.fetch_day(http, "TPE", "FRA", "2026-12-26", transfers=1)


# ── 单日摘要与矩阵 ──────────────────────────────────────────────────────


def test_summarize_failure_vs_empty(result):
    f = flt.Filters()
    err = service.summarize_day("2026-12-26", "out", None, f, error="HTTP 432")
    assert err["status"] == service.ERROR and err["error"] == "HTTP 432"
    none = service.summarize_day("2026-12-26", "out", service.DayRaw(itineraries=[], sub_code="AGG_NO_RESULT"), f)
    assert none["status"] == service.EMPTY and "没有航班" in none["note"]
    allbad = service.summarize_day("2026-12-26", "out", service.DayRaw(itineraries=result.itineraries), flt.Filters(max_duration_hours=1))
    assert allbad["status"] == service.EMPTY and allbad["rejected"] == 6 and allbad["top_rejected"]
    ok = service.summarize_day("2026-12-26", "out", service.DayRaw(itineraries=result.itineraries), f)
    assert ok["status"] == service.OK and ok["best"]["price"] == min(d["price"] for d in ok["top"])


def test_build_matrix():
    def day(st, price=None):
        return {"status": st, "best": {"price": price} if price else None}
    out = {"2026-12-26": day("ok", 500), "2026-12-27": day("error"), "2026-12-28": day("ok", 450)}
    ret = {"2026-12-27": day("ok", 300), "2027-01-08": day("empty")}
    m = service.build_matrix(out, ret, ["2026-12-26", "2026-12-27", "2026-12-28"], ["2026-12-27", "2027-01-08", "2027-01-09"])
    assert len(m) == 3 and all(len(r) == 3 for r in m)
    assert m[0][0]["status"] == "ok" and m[0][0]["price"] == 800
    assert m[0][1]["status"] == "empty"
    assert m[0][2]["status"] == "pending"
    assert m[1][1]["status"] == "error"          # 失败盖过无航班
    assert m[2][0]["status"] == "invalid"        # 回程早于去程
    assert [c["price"] for c in service.cheapest_cells(m)] == [800]


def test_validate_limits():
    q = service.Query("ow", "tpe", "fra", date(2026, 12, 1), date(2027, 1, 1))
    with pytest.raises(service.QueryError, match="超过上限 31"):
        service.validate(q, today=date(2026, 11, 22))
    q = service.Query("ow", "tpe", "fra", date(2026, 12, 24), date(2027, 1, 3))
    assert service.validate(q, today=date(2026, 11, 22)).origin == "TPE"
    rt = service.Query("rt", "TPE", "FRA", date(2026, 12, 20), date(2027, 1, 4), date(2027, 1, 8), date(2027, 1, 12))
    with pytest.raises(service.QueryError, match="去程区间 16 天"):
        service.validate(rt, today=date(2026, 11, 22))
    with pytest.raises(service.QueryError):
        service.validate(service.Query("ow", "TPE", "TPE", date(2026, 12, 24), date(2026, 12, 25)), today=date(2026, 11, 22))


def test_trip_url():
    assert service.trip_url("TPE", "FRA", "2026-12-26").startswith(
        "https://www.trip.com/flights/showfarefirst?dcity=tpe&acity=fra&ddate=2026-12-26&triptype=ow")
    assert service.trip_url("TPE", "FRA", "2026-12-26", "2027-01-10").endswith("&rdate=2027-01-10")
