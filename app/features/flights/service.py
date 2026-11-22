"""机票查询的业务逻辑：逐天查询（全局串行 + 限速 + 25 条补查 + 缓存）、日历/矩阵组装、DeepSeek 点评。

筛选规则在 filters.py（纯函数）；这里只负责「拿到每天的全部行程」和「组装」。
约定与踩坑见 docs/flights.md。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import httpx

from app.core.llm_json import extract_json_object
from app.core.settings import settings
from app.features.flights import client, filters as flt
from app.features.flights.client import Itinerary, TripError
from app.features.flights.repo import repo

logger = logging.getLogger(__name__)

OK, EMPTY, ERROR = "ok", "empty", "error"
TOP_N = 5   # 每天随 SSE 推给前端（悬浮提示/点评）的候选条数；完整明细走 /day


class QueryError(ValueError):
    """查询参数不合法（区间超限、日期倒置……），router 转成 400。"""


class Busy(RuntimeError):
    """已有一个长查询在跑。"""


# ── 查询参数 ──────────────────────────────────────────────────────────────


@dataclass
class Query:
    mode: str                      # "ow" | "rt"
    origin: str
    dest: str
    out_from: date
    out_to: date
    ret_from: Optional[date] = None
    ret_to: Optional[date] = None
    filters: flt.Filters = field(default_factory=flt.Filters)

    def out_days(self) -> list[date]:
        return _days(self.out_from, self.out_to)

    def ret_days(self) -> list[date]:
        return _days(self.ret_from, self.ret_to) if self.mode == "rt" and self.ret_from and self.ret_to else []


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _iata(code: str) -> str:
    c = (code or "").strip().upper()
    if len(c) != 3 or not c.isalpha():
        raise QueryError(f"机场/城市代码必须是 3 个字母（IATA）：{code!r}")
    return c


def validate(q: Query, today: Optional[date] = None) -> Query:
    """规范化并校验；不合法抛 QueryError。区间上限由后端强制（前端只是提示）。"""
    today = today or datetime.now(timezone.utc).date()
    q.origin, q.dest = _iata(q.origin), _iata(q.dest)
    if q.origin == q.dest:
        raise QueryError("出发地和目的地相同")
    if q.mode not in ("ow", "rt"):
        raise QueryError(f"未知模式：{q.mode}")

    def rng(a: Optional[date], b: Optional[date], label: str, cap: int) -> None:
        if not a or not b:
            raise QueryError(f"{label}日期没填全")
        if b < a:
            raise QueryError(f"{label}区间结束早于开始")
        if a < today - timedelta(days=1):
            raise QueryError(f"{label}区间包含已经过去的日期")
        n = (b - a).days + 1
        if n > cap:
            raise QueryError(f"{label}区间 {n} 天，超过上限 {cap} 天")

    if q.mode == "ow":
        rng(q.out_from, q.out_to, "出发", settings.flights_max_days_oneway)
    else:
        rng(q.out_from, q.out_to, "去程", settings.flights_max_days_roundtrip)
        rng(q.ret_from, q.ret_to, "回程", settings.flights_max_days_roundtrip)
        if q.ret_to < q.out_from:
            raise QueryError("回程区间整个早于去程区间")
    f = q.filters
    if f.max_transfers is not None and not 0 <= f.max_transfers <= 3:
        raise QueryError("最多中转次数只能是 0–3 或不限")
    return q


# ── 节流：全局串行 + 请求间隔 ─────────────────────────────────────────────

_pace_lock = asyncio.Lock()      # 所有 Trip 搜索请求排队，一次一个
_job_lock = asyncio.Lock()       # 同一时刻只允许一个长查询
_last_request_at = 0.0


async def _paced(coro_factory):
    """排队、等够间隔、再发。间隔在 settings 里，每次调用时读（可热改）。"""
    global _last_request_at
    async with _pace_lock:
        gap = random.uniform(settings.flights_request_interval_min, settings.flights_request_interval_max)
        wait = _last_request_at + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await coro_factory()
        finally:
            _last_request_at = time.monotonic()


def busy() -> bool:
    return _job_lock.locked()


# ── 单日：缓存 → 请求 → 25 条补查 ─────────────────────────────────────────


@dataclass
class DayRaw:
    """某天（某航向）的原始结果：全部行程，未过滤。"""
    itineraries: list[Itinerary]
    record_count: int = 0
    flags: list[str] = field(default_factory=list)
    sub_code: str = ""
    requests: int = 0          # 本次实际发出的 Trip 请求数（命中缓存 = 0）
    refilled: bool = False     # 是否触发了 25 条补查
    cached: bool = False
    fetched_at: Optional[datetime] = None
    warnings: list[str] = field(default_factory=list)


def needs_refill(count: int, n_items: int) -> bool:
    """顶到单次上限（25）= 有截断，要换排序方式补查。"""
    cap = settings.flights_page_cap
    return count == cap or n_items == cap


async def fetch_day(http: httpx.AsyncClient, origin: str, dest: str, day: str, *,
                    return_date: Optional[str] = None, transfers: Optional[int] = None,
                    use_cache: bool = True) -> DayRaw:
    """一天的全部行程。第一次请求失败抛 TripError；补查失败只记 warning（已有结果仍可用）。"""
    if use_cache:
        try:
            doc = repo().get(origin, dest, day, return_date, transfers)
        except Exception as e:  # 缓存坏了不该挡住查询
            logger.warning("flights 缓存读取失败：%s", e)
            doc = None
        if doc:
            return DayRaw(
                itineraries=[Itinerary.from_dict(x) for x in doc.get("itineraries") or []],
                record_count=doc.get("record_count", 0), flags=doc.get("flags") or [],
                sub_code=doc.get("sub_code", ""), refilled=doc.get("refilled", False),
                cached=True, fetched_at=_aware(doc.get("fetched_at")), warnings=doc.get("warnings") or [],
            )

    async def one(order: str) -> client.SearchResult:
        return await _paced(lambda: client.search(http, origin, dest, day, return_date=return_date,
                                                  order=order, max_transfers=transfers))

    first = await one(client.ORDERS[0])
    raw = DayRaw(itineraries=first.itineraries, record_count=first.record_count, flags=first.flags,
                 sub_code=first.sub_code, requests=1, fetched_at=datetime.now(timezone.utc))
    if first.skipped:
        raw.warnings.append(f"{first.skipped} 条行程解析失败被跳过")
    if needs_refill(first.record_count, len(first.itineraries)):
        raw.refilled = True
        lists = [first.itineraries]
        for order in client.ORDERS[1:]:
            try:
                r = await one(order)
                raw.requests += 1
                lists.append(r.itineraries)
            except TripError as e:
                raw.requests += 1
                raw.warnings.append(f"按 {order} 补查失败：{e}（结果可能不全）")
        raw.itineraries = flt.merge_unique(*lists)
    try:
        repo().put(origin, dest, day, return_date, transfers, {
            "itineraries": [it.to_dict() for it in raw.itineraries], "record_count": raw.record_count,
            "flags": raw.flags, "sub_code": raw.sub_code, "refilled": raw.refilled, "warnings": raw.warnings,
        })
    except Exception as e:
        logger.warning("flights 缓存写入失败：%s", e)
    return raw


def _aware(dt: Any) -> Optional[datetime]:
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ── 单日摘要（日历格 / 矩阵的一行一列） ───────────────────────────────────


def summarize_day(day: str, leg: str, raw: Optional[DayRaw], f: flt.Filters,
                  error: Optional[str] = None) -> dict:
    """→ 前端一格需要的全部信息。status：ok / empty（查到了但没有符合条件的）/ error（查询失败）。

    「empty」和「error」必须分开：后者是接口坏了，绝不能显示成「这天没航班」。
    """
    base = {"date": day, "leg": leg}
    if error is not None or raw is None:
        return {**base, "status": ERROR, "error": error or "未知错误", "requests": 0}
    ok, bad = flt.apply(raw.itineraries, f)
    out = {
        **base,
        "status": OK if ok else EMPTY,
        "total": len(raw.itineraries), "matched": len(ok), "rejected": len(bad),
        "best": ok[0] if ok else None,
        "top": ok[:TOP_N],
        "has_direct": any(len(it.segments) == 1 for it in raw.itineraries),
        "requests": raw.requests, "refilled": raw.refilled, "cached": raw.cached,
        "fetched_at": _iso(raw.fetched_at), "warnings": raw.warnings,
    }
    if not ok:
        if not raw.itineraries:
            out["note"] = {"AGG_NO_RESULT": "Trip 这天没有航班", "FILTER_NO_RESULT": "Trip 按服务端筛选无结果"}.get(
                raw.sub_code, f"Trip 返回 0 条{('（' + raw.sub_code + '）') if raw.sub_code else ''}")
        else:
            out["note"] = f"{len(bad)} 条全被条件排除"
            out["top_rejected"] = bad[:3]
    return out


# ── 往返矩阵 ──────────────────────────────────────────────────────────────


def build_matrix(out_days: dict[str, dict], ret_days: dict[str, dict],
                 out_order: list[str], ret_order: list[str]) -> list[list[dict]]:
    """行 = 去程日，列 = 回程日；每格 = 去程最低价 + 回程最低价（两张单程相加）。

    格的 status：invalid（回程早于去程）/ error（任一边查询失败）/ empty（任一边无符合条件的）/
    pending（任一边还没查）/ ok。优先级按这个顺序 —— 失败要盖过「无航班」，否则会被误读。
    """
    rows = []
    for o in out_order:
        row = []
        for r in ret_order:
            a, b = out_days.get(o), ret_days.get(r)
            if r < o:
                row.append({"out": o, "ret": r, "status": "invalid"})
                continue
            sts = [x["status"] if x else "pending" for x in (a, b)]
            if ERROR in sts:
                st = ERROR
            elif EMPTY in sts:
                st = EMPTY
            elif "pending" in sts:
                st = "pending"
            else:
                st = OK
            cell: dict[str, Any] = {"out": o, "ret": r, "status": st}
            if st == OK:
                cell["price"] = round(a["best"]["price"] + b["best"]["price"], 2)
                cell["out_best"], cell["ret_best"] = a["best"], b["best"]
            row.append(cell)
        rows.append(row)
    return rows


def cheapest_cells(matrix: list[list[dict]], n: int = 5) -> list[dict]:
    cells = [c for row in matrix for c in row if c["status"] == OK]
    return sorted(cells, key=lambda c: (c["price"], c["out"], c["ret"]))[:n]


# ── Trip 链接 ─────────────────────────────────────────────────────────────


def trip_url(origin: str, dest: str, day: str, return_date: Optional[str] = None) -> str:
    u = (f"https://www.trip.com/flights/showfarefirst?dcity={origin.lower()}&acity={dest.lower()}"
         f"&ddate={day}&triptype={'rt' if return_date else 'ow'}&class=y&quantity=1"
         f"&curr={settings.flights_currency}")
    return u + (f"&rdate={return_date}" if return_date else "")


# ── 长查询（SSE 事件流） ──────────────────────────────────────────────────

# 最近几次查询的结果，给点评用（进程内即可：点评是查完马上要的附加功能）
_recent: "OrderedDict[str, dict]" = OrderedDict()
_RECENT_MAX = 10


def _remember(qid: str, summary: dict) -> None:
    _recent[qid] = summary
    while len(_recent) > _RECENT_MAX:
        _recent.popitem(last=False)


def review_enabled() -> bool:
    return bool(settings.deepseek_api_key)


async def run_query(q: Query, http: Optional[httpx.AsyncClient] = None) -> AsyncIterator[tuple[str, dict]]:
    """逐天查询，查完一天推一天：yield (event, data)。

    事件：start（计划）→ day（每天一条，按完成顺序）→ done（汇总、矩阵、统计）。
    已有查询在跑时只推一条 busy。客户端断开时生成器被取消，锁在 finally 里释放。
    """
    try:
        await asyncio.wait_for(_job_lock.acquire(), timeout=5)
    except asyncio.TimeoutError:
        yield "busy", {"error": "已有一个查询在跑（全局串行，防止被 Trip 封），等它结束再查"}
        return
    own = http is None
    http = http or httpx.AsyncClient()
    t0 = time.monotonic()
    qid = uuid.uuid4().hex[:12]
    try:
        f = q.filters
        transfers = f.server_transfers()
        legs = [("out", q.origin, q.dest, d.isoformat()) for d in q.out_days()]
        legs += [("ret", q.dest, q.origin, d.isoformat()) for d in q.ret_days()]
        yield "start", {
            "query_id": qid, "mode": q.mode, "origin": q.origin, "dest": q.dest,
            "out_days": [d.isoformat() for d in q.out_days()], "ret_days": [d.isoformat() for d in q.ret_days()],
            "total_days": len(legs), "currency": settings.flights_currency,
            "review_enabled": review_enabled(),
        }
        days: dict[str, dict[str, dict]] = {"out": {}, "ret": {}}
        requests = refills = cached = errors = 0
        for i, (leg, a, b, day) in enumerate(legs):
            try:
                raw = await fetch_day(http, a, b, day, transfers=transfers)
                s = summarize_day(day, leg, raw, f)
                requests += raw.requests
                refills += int(raw.refilled and not raw.cached)
                cached += int(raw.cached)
            except TripError as e:
                # 第一条请求就失败：算一次请求，这天标「查询失败」
                requests += 1
                errors += 1
                s = summarize_day(day, leg, None, f, error=str(e))
                s["requests"] = 1
                logger.warning("flights %s→%s %s 查询失败：%s", a, b, day, e)
            s["link"] = trip_url(a, b, day)
            days[leg][day] = s
            yield "day", {**s, "done": i + 1, "total_days": len(legs), "requests_so_far": requests}

        elapsed = round(time.monotonic() - t0, 1)
        stats = {"days": len(legs), "requests": requests, "refills": refills, "cached_days": cached,
                 "errors": errors, "elapsed_s": elapsed}
        fetched = [s.get("fetched_at") for leg in days.values() for s in leg.values() if s.get("fetched_at")]
        done: dict[str, Any] = {"query_id": qid, "stats": stats,
                                "oldest_fetched_at": min(fetched) if fetched else None,
                                "newest_fetched_at": max(fetched) if fetched else None}
        summary: dict[str, Any] = {"query": _query_dict(q), "days": days}
        if q.mode == "rt":
            matrix = build_matrix(days["out"], days["ret"], [d.isoformat() for d in q.out_days()],
                                  [d.isoformat() for d in q.ret_days()])
            done["cheapest"] = cheapest_cells(matrix)
            summary["cheapest"] = done["cheapest"]
        _remember(qid, summary)
        logger.info("flights 查询 %s %s→%s %s：%s", qid, q.origin, q.dest, q.mode, stats)
        yield "done", done
    finally:
        _job_lock.release()
        if own:
            await http.aclose()


def _query_dict(q: Query) -> dict:
    return {"mode": q.mode, "origin": q.origin, "dest": q.dest,
            "out": [q.out_from.isoformat(), q.out_to.isoformat()],
            "ret": [q.ret_from.isoformat(), q.ret_to.isoformat()] if q.mode == "rt" and q.ret_from and q.ret_to else None,
            "filters": vars(q.filters)}


# ── 单日明细 / 真往返 ─────────────────────────────────────────────────────


async def day_detail(origin: str, dest: str, day: str, f: flt.Filters) -> dict:
    """某天的完整明细（符合的 + 被排除的带原因）。多半命中缓存；过期了就重查（同样走限速）。"""
    origin, dest = _iata(origin), _iata(dest)
    async with httpx.AsyncClient() as http:
        raw = await fetch_day(http, origin, dest, day, transfers=f.server_transfers())
    ok, bad = flt.apply(raw.itineraries, f)
    return {"date": day, "origin": origin, "dest": dest, "matched": ok, "rejected": bad,
            "fetched_at": _iso(raw.fetched_at), "cached": raw.cached, "requests": raw.requests,
            "refilled": raw.refilled, "warnings": raw.warnings, "link": trip_url(origin, dest, day)}


async def roundtrip_price(origin: str, dest: str, out_day: str, ret_day: str, f: flt.Filters) -> dict:
    """真往返（tripType=2）：返回的行程只含去程段、价格是往返总价。

    筛选只能套在去程段上 —— 回程段要等选定去程后才能查（Trip 网页也是两步），一期不复刻。
    """
    origin, dest = _iata(origin), _iata(dest)
    if ret_day < out_day:
        raise QueryError("回程早于去程")
    async with httpx.AsyncClient() as http:
        raw = await fetch_day(http, origin, dest, out_day, return_date=ret_day, transfers=f.server_transfers())
    ok, bad = flt.apply(raw.itineraries, f)
    cheapest_any = min((it.price for it in raw.itineraries), default=None)
    return {"out": out_day, "ret": ret_day, "best": ok[0] if ok else None, "matched": ok[:TOP_N],
            "matched_count": len(ok), "total": len(raw.itineraries), "cheapest_unfiltered": cheapest_any,
            "fetched_at": _iso(raw.fetched_at), "cached": raw.cached, "requests": raw.requests,
            "warnings": raw.warnings, "note": "回程段未按条件筛选（Trip 要选定去程后才给回程段）",
            "link": trip_url(origin, dest, out_day, ret_day)}


# ── DeepSeek 点评 ─────────────────────────────────────────────────────────

_REVIEW_SYSTEM = """你是帮人挑机票的助手。用户已经按自己的硬条件（中转次数、中转时长、行李直挂、总时长）筛好了每天的候选航班，
筛选和排序都已完成，你**不要**重新筛选，也不要编造数据里没有的航班或价格。
请根据数据写一段简短的中文点评和推荐：哪天/哪个组合最划算、代价是什么（中转地、中转时长、总时长、行李是否「推断直挂」需自行确认），
有没有「多花一点钱换直飞/更短」的更好选择，价格的规律（周末、节假日前后）。
baggage 字段：confirmed=确认直挂，inferred=推断直挂（同一张票但 Trip 没标记，建议下单前确认），recheck=需重新托运。
价格单位是 {cur}，会浮动，下单前以 Trip 详情页为准。
只输出 JSON：{{"summary": "一段 150 字以内的点评", "picks": [{{"label": "如 12/28 CI061", "why": "一句话理由"}}]}}，picks 最多 3 条。"""


def _brief(c: Optional[dict]) -> Optional[dict]:
    if not c:
        return None
    return {"flights": "+".join(c["flights"]), "price": c["price"], "duration": flt.fmt_minutes(c["duration"]),
            "dep": c["dep_time"][5:16], "arr": c["arr_time"][5:16],
            "layovers": [f"{l['airport']} {flt.fmt_minutes(l['minutes'])}" for l in c["layovers"]],
            "baggage": c["baggage"]}


def review_payload(summary: dict) -> dict:
    """发给模型的精简数据：每天最低价 + 前几名候选。"""
    out: dict[str, Any] = {"query": summary["query"]}
    for leg, days in summary["days"].items():
        if not days:
            continue
        out[leg] = [
            {"date": d, "status": s["status"], **({"error": s.get("error")} if s["status"] == ERROR else {}),
             "candidates": [_brief(c) for c in (s.get("top") or [])[:3]]}
            for d, s in sorted(days.items())
        ]
    if summary.get("cheapest"):
        out["cheapest_combos"] = [{"out": c["out"], "ret": c["ret"], "price": c["price"],
                                   "out_flight": _brief(c["out_best"]), "ret_flight": _brief(c["ret_best"])}
                                  for c in summary["cheapest"]]
    return out


async def review(query_id: str) -> dict:
    if not settings.deepseek_api_key:
        raise QueryError("没有配置 deepseek_api_key")
    summary = _recent.get(query_id)
    if not summary:
        raise QueryError("这次查询的结果已不在内存里（服务重启过或查询太多），重新查一次")
    payload = review_payload(summary)
    async with httpx.AsyncClient(timeout=90) as http:
        r = await http.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "temperature": 0.3, "max_tokens": 1200,
                  "response_format": {"type": "json_object"},
                  "messages": [
                      {"role": "system", "content": _REVIEW_SYSTEM.format(cur=settings.flights_currency)},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                  ]},
        )
    if r.status_code != 200:
        raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:200]}")
    reply = r.json()["choices"][0]["message"]["content"]
    obj = extract_json_object(reply)
    if not obj or not isinstance(obj.get("summary"), str):
        raise RuntimeError(f"DeepSeek 回复解析失败：{reply[:200]}")
    picks = [p for p in obj.get("picks") or [] if isinstance(p, dict)][:3]
    return {"summary": obj["summary"], "picks": picks}


# ── 自动补全 ──────────────────────────────────────────────────────────────


async def places(key: str) -> list[dict]:
    key = key.strip()
    if not key:
        return []
    async with httpx.AsyncClient() as http:
        return await client.poi_search(http, key)
