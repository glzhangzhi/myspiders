"""LLM 用量的「换成 DeepSeek API 要花多少」计价 —— 纯函数，不碰 Mongo / 文件 / 网络。

单价、峰谷窗口、价目历史一律来自 `liyuan_billing`（DeepSeek 计费的单一事实来源），
本模块只负责两件它不管的事：

**① 三个来源的 token 怎么映射到 DeepSeek 的三个计费项。**
账本行的字段是 disjoint 的（与 DSH 的 TokenUsage 同口径）：
  - 未命中 = `miss + cache_write` —— DeepSeek 没有「写缓存」这个计费项，
    Claude 报的 cache write 就是一段第一次见到的输入，按未命中算；
  - 命中   = `hit`；
  - 输出   = `output`（已含 reasoning，reasoning 不单独计价）。

**② 用哪个 DeepSeek 模型的价。**
  - 本来就跑在 DeepSeek 上的调用（酒馆、梨园、DSH 走 deepseek 的那部分）
    默认按它**实际用的模型**计价 —— 那是真实发生的账；
  - 其余（DSH 走 Claude 订阅的部分）没有真实账单，按页面开关选的
    `target`（flash / pro）估算 —— 这是「换成 API 会花多少」的假设；
  - `force=True` 时一律按 `target`，用来回答「全部都用 pro 会怎样」。

**与梨园旧页的口径差异（刻意）**：旧页在写入时冻结金额，这里**读取时计价**。
冻结是为了「调价那天历史统计不集体变脸」—— 而 `compute_cost` 本来就按调用时刻
选当时生效的价目（`PRICE_HISTORY` 只追加），所以读时计价同样不会变脸；
同时 flash/pro 开关要求按请求实时换价，冻结做不到。

**峰谷**一律按每条调用自己的 UTC 时刻判（`is_peak`），不做「挪到谷时」之类的假设：
机主的作息是固定的，估算要回答的是「按我实际的使用时刻，要花多少」。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from app.features.llm_usage.liyuan_billing import canonical_model, compute_cost

TARGETS = ("deepseek-flash", "deepseek-v4-pro")

# 面板上的行。DSH 按 provider 拆成两行：走 Claude 订阅的是「换成 DeepSeek 的估算」，
# 本来就走 DeepSeek 的是真实账 —— 两者性质不同，合在一行会把估算和实账混成一个数。
SOURCES = ("dsh", "dsh-deepseek", "tavern", "liyuan")

# 视为「本来就是 DeepSeek」的 provider 路由名（DSH 的 deepseek-official 与直连的 deepseek）。
DEEPSEEK_PROVIDERS = frozenset({"deepseek", "deepseek-official", "deepseek-account"})


def row_key(doc: Mapping) -> str:
    """一条调用归到面板的哪一行（见 SOURCES）。"""
    src = doc.get("source") or "unknown"
    if src == "dsh" and doc.get("provider") in DEEPSEEK_PROVIDERS:
        return "dsh-deepseek"
    return src


def deepseek_tokens(doc: Mapping) -> Optional[tuple[int, int, int]]:
    """把一条用量文档映射成 DeepSeek 的 (未命中, 命中, 输出)。

    token 字段缺失（调用在 usage 之前就中止）返回 None：计入请求次数，不计费。
    """
    miss = doc.get("miss")
    output = doc.get("output")
    if miss is None or output is None:
        return None
    return (
        int(miss) + int(doc.get("cache_write") or 0),
        int(doc.get("hit") or 0),
        int(output),
    )


def pricing_model(doc: Mapping, target: str, force: bool = False) -> str:
    """选出这条调用该按哪个 DeepSeek 模型计价（见模块 docstring ②）。"""
    if force or doc.get("provider") not in DEEPSEEK_PROVIDERS:
        return target
    return canonical_model(doc.get("model"))


@dataclass
class Bucket:
    """一组调用的金额与 token 汇总（USD）。"""

    requests: int = 0
    priced_requests: int = 0
    unpriced_requests: int = 0
    usd_miss: float = 0.0
    usd_hit: float = 0.0
    usd_output: float = 0.0
    tok_miss: int = 0
    tok_hit: int = 0
    tok_output: int = 0
    tok_cache_write: int = 0
    peak_requests: int = 0
    last_ts: Optional[dt.datetime] = None

    @property
    def usd_total(self) -> float:
        return self.usd_miss + self.usd_hit + self.usd_output

    def add(self, doc: Mapping, target: str, force: bool) -> None:
        self.requests += 1
        ts = doc.get("ts")
        if isinstance(ts, dt.datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        tokens = deepseek_tokens(doc)
        if tokens is None or not isinstance(ts, dt.datetime):
            return
        miss, hit, out = tokens
        cost = compute_cost(
            miss_tokens=miss, hit_tokens=hit, output_tokens=out,
            ts=ts, model=pricing_model(doc, target, force),
        )
        if not cost.priced:
            self.unpriced_requests += 1
            return
        self.priced_requests += 1
        self.peak_requests += 1 if cost.peak else 0
        self.usd_miss += cost.miss
        self.usd_hit += cost.hit
        self.usd_output += cost.output
        self.tok_miss += miss
        self.tok_hit += hit
        self.tok_output += out
        self.tok_cache_write += int(doc.get("cache_write") or 0)

    def composition(self) -> dict:
        """命中 / 未命中 / 输出 各自的金额、占比、token。占比之和为 100（总额为 0 时全 0）。"""
        total = self.usd_total
        parts = {
            "hit": (self.usd_hit, self.tok_hit),
            "miss": (self.usd_miss, self.tok_miss),
            "output": (self.usd_output, self.tok_output),
        }
        return {
            k: {"usd": usd, "pct": (usd / total * 100) if total > 0 else 0.0, "tokens": tok}
            for k, (usd, tok) in parts.items()
        }

    def hit_rate(self) -> Optional[float]:
        """**原始**缓存命中率 = 命中 / 全部输入（%）。cache write 算作没命中 ——
        与计价时把它并入未命中同一口径。没有输入时为 None。"""
        prompt = self.tok_hit + self.tok_miss
        return self.tok_hit / prompt * 100 if prompt > 0 else None

    def as_dict(self) -> dict:
        return {
            "usd": self.usd_total,
            "hit_rate": self.hit_rate(),
            "requests": self.requests,
            "priced_requests": self.priced_requests,
            "unpriced_requests": self.unpriced_requests,
            "peak_requests": self.peak_requests,
            "composition": self.composition(),
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
        }


@dataclass
class Summary:
    total: Bucket = field(default_factory=Bucket)
    by_source: dict[str, Bucket] = field(default_factory=dict)


def summarize(docs: Iterable[Mapping], target: str, force: bool = False) -> dict:
    """把一批用量文档汇总成面板要的结构。`target` 必须是 TARGETS 之一。"""
    if target not in TARGETS:
        raise ValueError(f"不支持的目标模型：{target}")
    s = Summary()
    for doc in docs:
        src = row_key(doc)
        s.total.add(doc, target, force)
        s.by_source.setdefault(src, Bucket()).add(doc, target, force)
    total = s.total.as_dict()
    sources = []
    for src in list(SOURCES) + sorted(k for k in s.by_source if k not in SOURCES):
        b = s.by_source.get(src, Bucket())
        d = b.as_dict()
        d["source"] = src
        d["share_pct"] = (b.usd_total / s.total.usd_total * 100) if s.total.usd_total > 0 else 0.0
        sources.append(d)
    return {"target": target, "force": force, "total": total, "by_source": sources}


def summarize_daily(
    docs: Iterable[Mapping], target: str, force: bool,
    first_day: dt.date, last_day: dt.date, tz: dt.tzinfo,
) -> list[dict]:
    """按 `tz` 的自然日汇总 [first_day, last_day]（两端都含），**每一天都出一行**。

    没有调用的日子给全 0 而不是省略 —— 这张图最大的用处是看出断档，
    省掉空日子会把断档从图上挤掉。日界按 `tz`（德国）划，与月份划界同一口径；
    计价的峰谷仍按每条调用自己的 UTC 时刻（`Bucket.add` 里），两件事互不相干。
    """
    if target not in TARGETS:
        raise ValueError(f"不支持的目标模型：{target}")
    days: dict[dt.date, dict[str, Bucket]] = {}
    totals: dict[dt.date, Bucket] = {}
    for doc in docs:
        ts = doc.get("ts")
        if not isinstance(ts, dt.datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        day = ts.astimezone(tz).date()
        if not first_day <= day <= last_day:
            continue
        totals.setdefault(day, Bucket()).add(doc, target, force)
        days.setdefault(day, {}).setdefault(row_key(doc), Bucket()).add(doc, target, force)
    out = []
    day = first_day
    while day <= last_day:
        t = totals.get(day, Bucket())
        out.append({
            "date": day.isoformat(),
            "usd": t.usd_total,
            "requests": t.requests,
            "hit_rate": t.hit_rate(),
            "by_source": {src: b.usd_total for src, b in days.get(day, {}).items()},
        })
        day += dt.timedelta(days=1)
    return out
