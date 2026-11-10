"""Claude 7 天窗口结束预测（claude_usage/forecast.py）的规格 —— 纯函数，不碰 Mongo。

最怕的都是「不报错、只给错数字」的失效：
  - resets_at 的亚秒级漂移被当成换周 → 一周裂成好几段，上周终值全错
  - 周内 utilization 偶发回落 → 曲线倒退，插值出负的消耗
  - 周初用得少 → 预测退化成线性外推那样严重高估（旧版的毛病）
  - 区间用了「本周之后」的误差 → 回测作弊，线上区间偏窄

    .venv/bin/pytest tests/test_claude_forecast.py -v
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from app.features.claude_usage.forecast import (
    DAY,
    H,
    WEEK,
    Forecaster,
    build_weeks,
    forecast,
    pick_pool,
)

UTC = dt.timezone.utc
TZ = ZoneInfo("Europe/Berlin")
# 真实账户的重置时刻：周二 10:00 UTC
FIRST_RESET = dt.datetime(2026, 9, 1, 10, 0, tzinfo=UTC).timestamp()


def _iso(ts: float, drift: float = 0.0) -> str:
    return dt.datetime.fromtimestamp(ts + drift, UTC).isoformat()


def _weeks(n: int, per_day: list[float], step_h: float = 0.5, until: float | None = None) -> list[dict]:
    """n 个连续周、每 step_h 小时一个样本；每天按 per_day[星期几] 均匀消耗。

    resets_at 交替 ±0.1 秒漂移，模拟 API 的 09:59:59.9 / 10:00:00。
    """
    out = []
    for k in range(n):
        reset = FIRST_RESET + k * WEEK
        start = reset - WEEK
        t = start + step_h * H
        while t < reset and (until is None or t <= until):
            days = (t - start) / DAY
            u = sum(per_day[i % 7] for i in range(int(days))) + per_day[int(days) % 7] * (days % 1)
            drift = -0.1 if len(out) % 2 else 0.0
            out.append({"ts": t, "seven_day": {"utilization": round(u), "resets_at": _iso(reset, drift)}})
            t += step_h * H
    return out


def test_resets_at_drift_stays_one_week():
    s = _weeks(2, [3] * 7)
    weeks = build_weeks(s)
    assert len(weeks) == 2
    assert all(w.final is not None for w in weeks)


def test_within_week_drop_is_monotonised():
    reset = FIRST_RESET
    s = [{"ts": reset - 3 * DAY + i * H, "seven_day": {"utilization": u, "resets_at": _iso(reset)}}
         for i, u in enumerate([10, 24, 7, 25])]
    (w,) = build_weeks(s)
    assert [p[1] for p in w.pts] == [10, 24, 24, 25]


def test_steady_usage_predicts_close_to_truth():
    per_day = [8, 8, 8, 8, 8, 2, 2]  # 工作日重、周末轻；周终值 = 44
    history = _weeks(6, per_day)
    # 第 7 周刚过两天
    now = FIRST_RESET + 5 * WEEK + 2 * DAY
    cur = _weeks(7, per_day, until=now)[len(history):]
    r = forecast(history + cur, now, TZ)
    assert r is not None
    assert abs(r["predicted_end"] - 44) <= 3
    assert r["low"] <= r["predicted_end"] <= r["high"]


def test_slow_start_is_not_linearly_extrapolated():
    """周初几乎没用：线性外推会给 ~0，过往周都在后半周猛用；预测应跟着历史走。"""
    per_day = [0, 0, 1, 1, 15, 15, 15]  # 窗口从周二开始计，最后三天是重度期
    history = _weeks(5, per_day)
    now = FIRST_RESET + 4 * WEEK + 1.5 * DAY
    cur = _weeks(6, per_day, until=now)[len(history):]
    r = forecast(history + cur, now, TZ)
    assert r["predicted_end"] > 35


def test_prediction_never_below_current():
    history = _weeks(4, [1] * 7)
    heavy = _weeks(5, [20] * 7, until=FIRST_RESET + 3 * WEEK + 4 * DAY)[len(history):]
    r = forecast(history + heavy, FIRST_RESET + 3 * WEEK + 4 * DAY, TZ)
    assert r["predicted_end"] >= r["current"]
    assert r["low"] >= r["current"]


def test_residual_pool_only_uses_finished_weeks():
    samples = _weeks(6, [5] * 7)
    fc = Forecaster(samples, TZ)
    cut = fc.weeks[3].start + 600
    # 截断在第 4 周开头：误差池只能来自前 3 周，不能偷看第 4 周及以后的终值
    used = fc.residuals(before=cut)
    assert used
    assert len(used) * 2 == len(fc.residuals(before=fc.weeks[-1].reset + 1))


def test_pool_widens_when_sparse():
    res = [(6.0, 1.0)] * 3 + [(4.5, 2.0)] * 6
    assert len(pick_pool(res, 5.8)) == 9  # ±1 天只有 3 条，放宽到 ±2 天


def test_no_data_returns_none():
    assert forecast([], FIRST_RESET, TZ) is None
    # 只有已结束的周：当前窗口还没有样本
    s = _weeks(2, [3] * 7)
    assert forecast(s, FIRST_RESET + 3 * WEEK, TZ) is None
