"""DeepSeek 计费口径 — **单一事实来源**。

本模块是纯函数集合：不碰 Mongo、不碰文件、不发网络请求，只做
「时间 + token 数 → 钱」的换算。所有计费相关的常量和判定都必须写在这里，
其它模块一律调用本模块，不要自己复制单价或窗口规则。

## 三条核心口径

**① 峰谷窗口用 UTC 定义，不是北京时间。**
官方原文（https://api-docs.deepseek.com/quick_start/pricing/）：
*Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Monday through Friday,
excluding Chinese public holidays. All other hours are off-peak, including
weekends and Chinese public holidays in full.*

这一条消灭了整个时区问题 —— 存储本来就是 UTC（项目既有约定），判峰谷
直接比 UTC 小时，无需换算、不受夏令时影响。早期 2025 年的「北京时间
00:30~08:30 错峰」规则已被 2026-08 那轮改版取代，**别再用它**。

**② 价格在写入时冻结，绝不在读取时重算。**
官方明示 *prices may vary and DeepSeek reserves the right to adjust them*，
且 2025-2026 已至少调价四次（25-02 错峰 / 25-05 降价 / 26-08-17 涨价并引入
峰谷 / 26-08-23 周末全谷）。历史记录必须带着当时用的单价一起存，否则
某天调价会让所有历史统计集体变脸 —— 这是记账系统的常识：订单存成交价，
不去 join 商品表的当前价。

**③ 谷价恒为峰价的一半。**
官方原文 *Off-peak rates are half of the peak rates*。所以价目表只存峰价，
谷价由 `/2` 导出，不单独维护（少一半出错机会）。

## 已知偏差：中国公休日

官方规定公休日全天谷价，但**不提供 API**。硬编码节假日表会过期、会算错。
本模块只按「工作日 + UTC 小时」判定，即**把公休日当峰时算**。

这是刻意选择的保守偏差：它只会让估算**偏高**，绝不会偏低。宁可显示的钱比
真实账单多，也不要反过来给人「花得比实际少」的错觉。需要精确时再补一张
可手工编辑的节假日表，判定函数留了 `holidays` 参数作为扩展点。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional

# ── 峰时窗口（UTC 小时，左闭右开）──
# 对应德国夏令时 03:00-06:00 与 08:00-12:00；冬令时各减一小时。
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


@dataclass(frozen=True)
class PriceTable:
    """某个模型在某段时间内生效的价目（USD / 1M tokens，**峰时价**）。

    谷价由 `off_peak()` 按官方规则除以 2 导出，不单独存储。
    `effective_from` 用于按调用时刻选出当时生效的那一份（UTC、tz-aware）。
    """

    model: str
    effective_from: dt.datetime
    miss: float   # 输入 · 缓存未命中
    hit: float    # 输入 · 缓存命中
    output: float  # 输出（含 reasoning，reasoning 不单独计价）
    currency: str = "USD"
    note: str = ""

    def rates(self, peak: bool) -> tuple[float, float, float]:
        """返回 (miss, hit, output) 三项单价；谷时自动折半。"""
        if peak:
            return (self.miss, self.hit, self.output)
        return (self.miss / 2, self.hit / 2, self.output / 2)


# ── 价目历史：按 effective_from 升序，**只追加不修改** ──
#
# 调价时在末尾加一行，永远不要改动或删除已有行 —— 历史记录靠它们复算。
PRICE_HISTORY: tuple[PriceTable, ...] = (
    PriceTable(
        model="deepseek-flash",
        effective_from=dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc),
        miss=0.30,
        hit=0.006,
        output=1.20,
        note="2026-08-17 起：全面上调并引入峰谷定价；08-23 起周末全天谷价",
    ),
    PriceTable(
        model="deepseek-v4-pro",
        effective_from=dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc),
        miss=0.66,
        hit=0.022,
        output=1.98,
        note="Pro 档，当前未使用，列出以备切换。⚠️ 这一行录成了**谷价**（官方页峰价是 "
             "1.32 / 0.044 / 3.96），而本表约定存峰价，所以本行生效期间的 pro 金额被算低一半。"
             "按「只追加」规则不改它，由下一行纠正",
    ),
    PriceTable(
        model="deepseek-v4-pro",
        # 纠正上一行。生效点取那一行的同一时刻再加 1 微秒：`price_for` 取最后一个
        # effective_from <= ts 的价目，于是此后**全部**调用都落在这一行上 —— 包括
        # 2026-08-17 以后的历史。这正是想要的：那一行是录错的数，不是当时真实的价。
        effective_from=dt.datetime(2026, 8, 17, 0, 0, 0, 1, tzinfo=dt.timezone.utc),
        miss=1.32,
        hit=0.044,
        output=3.96,
        note="2026-11-02 核对官方价格页：v4-pro 峰价 miss 1.32 / hit 0.044 / output 3.96",
    ),
)

# 旧模型名 → 现模型名。
# 官方注明 deepseek-v4-flash / deepseek-v4-flash-vision-exp 已退役，
# 请求由 DeepSeek-V4.1-Flash 承接并**按 Flash 价计费**，故直接归一。
MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
}

DEFAULT_MODEL = "deepseek-flash"


def canonical_model(model: Optional[str]) -> str:
    """把历史/别名模型名归一到当前计费用的名字。"""
    if not model:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(model, model)


def is_peak(
    ts: dt.datetime,
    holidays: Optional[Iterable[dt.date]] = None,
) -> bool:
    """判断某一时刻是否落在峰时（双倍价）。

    Args:
        ts: 调用时刻，必须是 tz-aware。naive datetime 会被拒绝 —— 默默当成
            UTC 是这类代码最容易埋的雷，宁可炸在调用点。
        holidays: 可选的中国公休日集合（date）。不传则不考虑公休日，
            即把公休日算作峰时（保守偏高，见模块 docstring）。

    Returns:
        True 表示峰时。周末一律 False。
    """
    if ts.tzinfo is None:
        raise ValueError("is_peak() 需要 tz-aware 的时间；naive datetime 会导致静默错账")

    u = ts.astimezone(dt.timezone.utc)

    # 周末全天谷价（2026-08-23 起）
    if u.weekday() >= 5:
        return False

    # 中国公休日全天谷价。注意：官方按北京时间划分日历日，故用 UTC+8 取日期。
    if holidays:
        cn_date = u.astimezone(dt.timezone(dt.timedelta(hours=8))).date()
        if cn_date in set(holidays):
            return False

    hour = u.hour
    return any(start <= hour < end for start, end in PEAK_WINDOWS_UTC)


def price_for(model: str, ts: dt.datetime) -> Optional[PriceTable]:
    """选出 `ts` 时刻对该模型生效的价目（最后一个 effective_from <= ts 的）。

    调用时刻早于所有价目的生效时间时返回 None —— 调用方应把这条记录标记为
    「无法计价」，而不是拿最早的价目硬套。
    """
    if ts.tzinfo is None:
        raise ValueError("price_for() 需要 tz-aware 的时间")

    name = canonical_model(model)
    candidates = [
        p for p in PRICE_HISTORY if p.model == name and p.effective_from <= ts
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.effective_from)


@dataclass(frozen=True)
class CostBreakdown:
    """一次调用的计费结果。所有金额单位 USD。

    `saved_if_offpeak` 是这条记录**挪到谷时能省下的钱**：峰时记录等于
    金额的一半，谷时记录为 0。它是页面首屏「错峰可省」的来源 —— 也是
    整个统计里唯一能直接改变用户行为的数字。
    """

    miss: float
    hit: float
    output: float
    total: float
    peak: bool
    priced: bool                 # False = 找不到适用价目，金额均为 0
    price_model: str = ""
    price_effective_from: Optional[dt.datetime] = None

    @property
    def saved_if_offpeak(self) -> float:
        return self.total / 2 if self.peak else 0.0


def compute_cost(
    *,
    miss_tokens: int,
    hit_tokens: int,
    output_tokens: int,
    ts: dt.datetime,
    model: str = DEFAULT_MODEL,
    holidays: Optional[Iterable[dt.date]] = None,
) -> CostBreakdown:
    """计算一次 API 调用的花费。

    token 口径（与梨园写入 jsonl 的 usage 对象对齐）：
      - miss_tokens  = usage.input      本次 prompt 中**未命中**前缀缓存的部分
      - hit_tokens   = usage.cacheRead  本次 prompt 中**命中**的部分
      - output_tokens = usage.output    补全 token，**已包含** reasoning

    注意 `usage.input` 不是"完整 prompt"，完整 prompt = input + cacheRead。
    这是最容易搞错的一点：拿 input 当 prompt 会让命中率算成 0。
    """
    peak = is_peak(ts, holidays)
    table = price_for(model, ts)

    if table is None:
        return CostBreakdown(
            miss=0.0, hit=0.0, output=0.0, total=0.0,
            peak=peak, priced=False,
        )

    r_miss, r_hit, r_out = table.rates(peak)
    c_miss = miss_tokens * r_miss / 1_000_000
    c_hit = hit_tokens * r_hit / 1_000_000
    c_out = output_tokens * r_out / 1_000_000

    return CostBreakdown(
        miss=c_miss,
        hit=c_hit,
        output=c_out,
        total=c_miss + c_hit + c_out,
        peak=peak,
        priced=True,
        price_model=table.model,
        price_effective_from=table.effective_from,
    )


def next_window_change(ts: dt.datetime) -> tuple[bool, dt.datetime]:
    """返回 (当前是否峰时, 下一次峰谷切换的时刻)。

    供页面角标「现在是谷时，还剩 N 小时」用 —— 这是页面上唯一的实时信息。
    逐小时向前扫，最多扫 8 天（足够跨过周末这段最长的连续谷时）。
    """
    if ts.tzinfo is None:
        raise ValueError("next_window_change() 需要 tz-aware 的时间")

    cur = is_peak(ts)
    u = ts.astimezone(dt.timezone.utc)
    probe = u.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)

    for _ in range(8 * 24):
        if is_peak(probe) != cur:
            return cur, probe
        probe += dt.timedelta(hours=1)

    # 理论上不可达：一周内必定发生切换
    return cur, probe
