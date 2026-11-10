"""7 天窗口「周期结束时用到多少」的预测 —— 纯函数，不碰 Mongo、不读时钟。

方法（2026-11 用 10 个完整周回测选出，见 docs/claude_usage.md）：三个信号等权平均

  A 周几画像：近 30 天每个周几（德国时间）的平均消耗，按剩余时段累加。
     折算周数用「有消耗的天数 / 7」，不用日历周数 —— 机器断档的日子会被当成「没用」，
     系统性低估（回测平均误差 6.9 → 10.7）。
  B 上周剩余：上一个完整周在「同一相对时刻」之后又用了多少。只看上周：用量水平在漂移，
     近 4 周 9.5、全部历史 11.3，越老越拖后腿。
  C 滚动 7 天：过去 168 小时的实际消耗 × 剩余时间占比。

不用线性外推（当前值 / 已过时间 × 剩余时间）：周初用得少它就严重高估，回测平均误差 15.8、
偏差 +14，是旧版预测（线性 ↔ 画像混合）不准的主因。

区间不是假设分布，而是**本方法自己在过去每一周的真实误差**：每周每 6 小时一个截断点、只用
截断点之前的数据预测，记下（剩余天数, 预测 − 终值）。当前预测按相近剩余天数取这些误差的
分位数。周初（剩 5 天以上）误差约 ±10 个百分点，这是「这周会不会是重度周」本来就不可知，
所以前端必须同时展示区间。

样本格式即 claude_usage_history 文档：{"ts": epoch 秒, "seven_day": {"utilization", "resets_at"}}。
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

H = 3600.0
DAY = 86400.0
WEEK = 7 * DAY

KEY = "seven_day"
PROFILE_DAYS = 30
# 截断点距最近样本超过这么久就不评估（机器断档，当时的「当前值」不可知）
CUTOFF_MAX_STALENESS = H
# 窗口最后一个样本距重置不超过这么久，才把它当作该周的终值
FINAL_MAX_GAP = 2 * H
# 一周至少这么多个样本才拿来当「上周」与回测对象（太稀的周曲线靠插值，不可信）
MIN_WEEK_SAMPLES = 20
# 误差池：最多用最近这么多周；同档剩余天数内少于这么多条就放宽一档
RESIDUAL_WEEKS = 16
MIN_POOL = 8
# 分位：回测里 10/90 分位只覆盖了 73%，取 5/95 实测接近 80%（见 docs）
Q_LO, Q_HI = 0.05, 0.95


@dataclass
class Week:
    reset: float
    pts: list[tuple[float, float]] = field(default_factory=list)  # (ts, 单调化后的 util)
    final: float | None = None

    @property
    def start(self) -> float:
        return self.reset - WEEK

    def at(self, t: float) -> float | None:
        """t 时刻的累计用量（样本间线性插值，周起点视为 0）；超出已知范围返回 None。"""
        pts = [(self.start, 0.0), *self.pts]
        if self.final is not None:
            pts.append((self.reset, self.final))
        if t <= pts[0][0]:
            return 0.0
        if t > pts[-1][0]:
            return None
        ts = [p[0] for p in pts]
        i = bisect.bisect_left(ts, t)
        if ts[i] == t:
            return pts[i][1]
        (t0, u0), (t1, u1) = pts[i - 1], pts[i]
        return u0 + (u1 - u0) * (t - t0) / (t1 - t0)

    def known_until(self) -> float:
        return self.reset if self.final is not None else (self.pts[-1][0] if self.pts else self.start)

    def sample_before(self, t: float) -> tuple[float, float] | None:
        best = None
        for p in self.pts:
            if p[0] > t:
                break
            best = p
        return best


def _iso_ts(s) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def build_weeks(samples: list[dict]) -> list[Week]:
    """按 resets_at 把样本分到各周。resets_at 会在亚秒级漂移（09:59:59.9 / 10:00:00），
    差 10 分钟以内视为同一周。周内偶发的 utilization 回落做单调化（取累计最大值）。"""
    weeks: list[Week] = []
    for s in sorted(samples, key=lambda x: x.get("ts") or 0):
        w = s.get(KEY) or {}
        u, r, ts = w.get("utilization"), _iso_ts(w.get("resets_at")), s.get("ts")
        if not isinstance(u, (int, float)) or r is None or ts is None:
            continue
        wk = next((x for x in reversed(weeks[-3:]) if abs(x.reset - r) <= 600), None)
        if wk is None:
            wk = Week(reset=r)
            weeks.append(wk)
        prev = wk.pts[-1][1] if wk.pts else 0.0
        wk.pts.append((float(ts), max(prev, float(u))))
    weeks.sort(key=lambda w: w.reset)
    for wk in weeks:
        if wk.pts and wk.reset - wk.pts[-1][0] <= FINAL_MAX_GAP:
            wk.final = wk.pts[-1][1]
    return weeks


def build_deltas(samples: list[dict]) -> tuple[list[float], list[float]]:
    """相邻样本的消耗差值 (ts 列表, Δ 列表)，与 service._compute_deltas 同一口径：
    同一周内取差值，换周后新周首个样本的值整个算作消耗。"""
    tss: list[float] = []
    ds: list[float] = []
    prev: tuple[float, float] | None = None
    for s in sorted(samples, key=lambda x: x.get("ts") or 0):
        w = s.get(KEY) or {}
        u, r, ts = w.get("utilization"), _iso_ts(w.get("resets_at")), s.get("ts")
        if not isinstance(u, (int, float)) or r is None or ts is None:
            prev = None
            continue
        if prev is not None:
            d = (u - prev[0]) if abs(r - prev[1]) <= 600 else u
            if d > 0:
                tss.append(float(ts))
                ds.append(float(d))
        prev = (float(u), r)
    return tss, ds


class Forecaster:
    def __init__(self, samples: list[dict], tz: tzinfo):
        self.tz = tz
        self.weeks = build_weeks(samples)
        self.d_ts, self.d_val = build_deltas(samples)

    def _usable(self, w: Week) -> bool:
        return w.final is not None and w.final > 0 and len(w.pts) >= MIN_WEEK_SAMPLES

    # ── 三个信号：返回「从 t 到 reset 还会再用多少」──

    def _profile_add(self, t: float, reset: float) -> float | None:
        lo = bisect.bisect_left(self.d_ts, t - PROFILE_DAYS * DAY)
        hi = bisect.bisect_right(self.d_ts, t)
        if hi <= lo:
            return None
        totals = [0.0] * 7
        days = set()
        for i in range(lo, hi):
            dt = datetime.fromtimestamp(self.d_ts[i], self.tz)
            totals[dt.weekday()] += self.d_val[i]
            days.add(dt.date())
        weeks = max(1.0, len(days) / 7)
        avg = [x / weeks for x in totals]
        add, x = 0.0, t
        while x < reset:
            dt = datetime.fromtimestamp(x, self.tz)
            nxt = (dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
            e = min(reset, nxt)
            add += avg[dt.weekday()] * (e - x) / DAY
            x = e
        return add

    def _last_week_add(self, cur: Week, t: float) -> float | None:
        prev = [w for w in self.weeks if w.reset < cur.reset - 600 and self._usable(w)]
        if not prev:
            return None
        lw = prev[-1]
        so_far = lw.at(lw.start + (t - cur.start))
        return None if so_far is None else max(0.0, lw.final - so_far)

    def _rolling_add(self, t: float, reset: float) -> float | None:
        a = t - WEEK
        total, covered = 0.0, False
        for w in self.weeks:
            lo, hi = max(a, w.start), min(t, w.reset, w.known_until())
            if hi <= lo:
                continue
            ca, cb = w.at(lo), w.at(hi)
            if ca is None or cb is None:
                continue
            total += cb - ca
            covered = True
        return total * (reset - t) / WEEK if covered else None

    def predict_at(self, cur: Week, t: float, u: float) -> tuple[float, dict] | None:
        signals = {
            "profile": self._profile_add(t, cur.reset),
            "last_week": self._last_week_add(cur, t),
            "rolling": self._rolling_add(t, cur.reset),
        }
        adds = [v for v in signals.values() if v is not None]
        if not adds:
            return None
        pred = u + statistics.fmean(adds)
        return pred, {k: (None if v is None else round(u + v, 1)) for k, v in signals.items()}

    # ── 自我回测：误差池 ──

    def residuals(self, before: float) -> list[tuple[float, float]]:
        """对 before 之前结束的每个完整周回放预测，返回 [(剩余天数, 预测 − 终值)]。"""
        out: list[tuple[float, float]] = []
        done = [w for w in self.weeks if w.reset <= before and self._usable(w)][-RESIDUAL_WEEKS:]
        for w in done:
            for k in range(1, 28):
                s = w.sample_before(w.start + k * 6 * H)
                if s is None or w.start + k * 6 * H - s[0] > CUTOFF_MAX_STALENESS or s[0] - w.start < 3 * H:
                    continue
                r = self.predict_at(w, s[0], s[1])
                if r is not None:
                    out.append(((w.reset - s[0]) / DAY, r[0] - w.final))
        return out


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    pos = q * (len(xs) - 1)
    i = int(pos)
    j = min(i + 1, len(xs) - 1)
    return xs[i] + (xs[j] - xs[i]) * (pos - i)


def pick_pool(residuals: list[tuple[float, float]], rem_days: float) -> list[float]:
    for width in (1.0, 2.0):
        pool = [e for d, e in residuals if abs(d - rem_days) < width]
        if len(pool) >= MIN_POOL:
            return pool
    return []


def forecast(samples: list[dict], now: float, tz: tzinfo,
             residuals: list[tuple[float, float]] | None = None) -> dict | None:
    """当前 7 天窗口的结束预测。residuals 可由调用方缓存后传入（每周才变一次）。"""
    fc = Forecaster(samples, tz)
    if not fc.weeks:
        return None
    cur = fc.weeks[-1]
    if not cur.pts or cur.reset <= now:
        return None
    t, u = cur.pts[-1]
    r = fc.predict_at(cur, t, u)
    if r is None:
        return None
    pred, signals = r
    pred = max(u, pred)
    rem_days = (cur.reset - t) / DAY
    if residuals is None:
        residuals = fc.residuals(before=cur.start + 600)
    pool = pick_pool(residuals, rem_days)

    out = {
        "current": round(u, 1),
        "predicted_end": round(pred, 1),
        "signals": signals,
        "resets_at": cur.reset,
        "as_of": t,
        "low": None, "high": None, "p_cap": None,
        "pool": len(pool),
        "weeks": sum(1 for w in fc.weeks if fc._usable(w) and w.reset < cur.reset),
    }
    if pool:
        # 误差 e = 预测 − 终值 → 终值 = 预测 − e
        out["low"] = round(max(u, pred - _quantile(pool, Q_HI)), 1)
        out["high"] = round(max(u, pred - _quantile(pool, Q_LO)), 1)
        out["p_cap"] = round(sum(1 for e in pool if pred - e >= 100) / len(pool), 2)
    return out
