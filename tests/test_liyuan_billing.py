"""守住 DeepSeek 计费口径 — `app/features/llm_usage/liyuan_billing.py` 的可执行规格。

这是全项目少数几个「算错了不会报错、只会悄悄给出错数字」的地方，
所以纯函数全部覆盖。跑：

    .venv/bin/pytest tests/test_liyuan_billing.py -v

重点守三件事：
  1. 峰谷窗口按 **UTC** 判（不是北京时间）—— 搞错等于全站账目翻倍或减半
  2. naive datetime 必须炸，不能默默当 UTC
  3. 价目按 effective_from 选取，调价不影响历史
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.features.llm_usage.liyuan_billing import (
    MODEL_ALIASES,
    PRICE_HISTORY,
    canonical_model,
    compute_cost,
    is_peak,
    next_window_change,
    price_for,
)

UTC = dt.timezone.utc


def _utc(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=UTC)


# ── 峰谷判定 ──


class TestIsPeak:
    """官方：Peak = 01:00-04:00 和 06:00-10:00 UTC，周一至周五。"""

    # 2026-09-21 是周一
    @pytest.mark.parametrize(
        "hour,expected",
        [
            (0, False),   # 窗口前
            (1, True),    # 第一个窗口左边界（闭）
            (3, True),
            (4, False),   # 右边界（开）—— 04:00 整已是谷时
            (5, False),   # 两窗口之间的缝隙
            (6, True),    # 第二个窗口左边界
            (9, True),
            (10, False),  # 右边界（开）
            (15, False),
            (23, False),
        ],
    )
    def test_weekday_hours(self, hour, expected):
        assert is_peak(_utc(2026, 9, 21, hour)) is expected

    def test_boundaries_are_half_open(self):
        """边界必须左闭右开，否则 04:00:00 那一秒会被算成峰时。"""
        assert is_peak(_utc(2026, 9, 21, 3, 59)) is True
        assert is_peak(_utc(2026, 9, 21, 4, 0)) is False
        assert is_peak(_utc(2026, 9, 21, 9, 59)) is True
        assert is_peak(_utc(2026, 9, 21, 10, 0)) is False

    def test_weekend_is_always_offpeak(self):
        """2026-08-23 起周末全天谷价，即使落在峰时小时内。"""
        # 2026-09-26 周六 / 09-27 周日，取峰时小时 02:00 与 08:00
        for day in (26, 27):
            for hour in (2, 8):
                assert is_peak(_utc(2026, 9, day, hour)) is False, f"9/{day} {hour}:00"

    def test_naive_datetime_is_rejected(self):
        """naive 时间默默当 UTC 是最容易埋的错账雷，必须炸在调用点。"""
        with pytest.raises(ValueError, match="tz-aware"):
            is_peak(dt.datetime(2026, 9, 21, 2))

    def test_non_utc_tz_is_converted_not_assumed(self):
        """传德国时间必须先换算成 UTC 再判，不能拿本地小时去比。"""
        berlin = dt.timezone(dt.timedelta(hours=2))  # CEST
        # 德国 10:00 CEST = UTC 08:00 → 峰时
        assert is_peak(dt.datetime(2026, 9, 21, 10, tzinfo=berlin)) is True
        # 德国 17:00 CEST = UTC 15:00 → 谷时
        assert is_peak(dt.datetime(2026, 9, 21, 17, tzinfo=berlin)) is False

    def test_chinese_holiday_optional_override(self):
        """传了公休日则该日全天谷价；不传则按峰时算（保守偏高）。"""
        ts = _utc(2026, 10, 1, 2)  # 周四 02:00 UTC = 峰时小时
        assert is_peak(ts) is True
        # 按北京时间该时刻是 10-01 10:00，属国庆
        assert is_peak(ts, holidays=[dt.date(2026, 10, 1)]) is False

    def test_holiday_uses_beijing_date_not_utc_date(self):
        """公休日按北京时间的日历日划分，跨 UTC 日界时不能用 UTC 日期。"""
        # UTC 09-30 17:00 = 北京 10-01 01:00 → 属于国庆当天
        ts = _utc(2026, 9, 30, 17)
        assert is_peak(ts, holidays=[dt.date(2026, 10, 1)]) is False


# ── 价目选取 ──


class TestPriceFor:
    def test_picks_table_effective_at_timestamp(self):
        p = price_for("deepseek-flash", _utc(2026, 9, 21, 12))
        assert p is not None
        assert p.model == "deepseek-flash"
        assert p.miss == 0.30

    def test_returns_none_before_any_price_exists(self):
        """早于所有价目 → None，调用方须标记为无法计价，不能硬套最早价目。"""
        assert price_for("deepseek-flash", _utc(2020, 1, 1)) is None

    def test_unknown_model_returns_none(self):
        assert price_for("gpt-9-imaginary", _utc(2026, 9, 21)) is None

    def test_price_history_is_sorted_and_utc(self):
        """价目表必须全是 tz-aware，否则比较会抛异常。"""
        for p in PRICE_HISTORY:
            assert p.effective_from.tzinfo is not None, p.model

    def test_offpeak_is_exactly_half(self):
        """官方：Off-peak rates are half of the peak rates。"""
        p = price_for("deepseek-flash", _utc(2026, 9, 21, 12))
        peak = p.rates(peak=True)
        off = p.rates(peak=False)
        assert off == tuple(x / 2 for x in peak)


class TestCanonicalModel:
    def test_legacy_names_map_to_flash(self):
        """官方注明退役名仍被接受并按 Flash 价计费。"""
        for legacy in ("deepseek-v4-flash", "deepseek-chat"):
            assert canonical_model(legacy) == "deepseek-flash"

    def test_unknown_name_passes_through(self):
        assert canonical_model("deepseek-v4-pro") == "deepseek-v4-pro"

    def test_empty_falls_back_to_default(self):
        assert canonical_model(None) == "deepseek-flash"
        assert canonical_model("") == "deepseek-flash"

    def test_aliases_all_point_at_known_models(self):
        known = {p.model for p in PRICE_HISTORY}
        for target in MODEL_ALIASES.values():
            assert target in known, target


# ── 成本计算 ──


class TestComputeCost:
    def test_peak_rates(self):
        c = compute_cost(
            miss_tokens=1_000_000,
            hit_tokens=1_000_000,
            output_tokens=1_000_000,
            ts=_utc(2026, 9, 21, 2),  # 周一 02:00 UTC = 峰
        )
        assert c.peak is True
        assert c.priced is True
        assert c.miss == pytest.approx(0.30)
        assert c.hit == pytest.approx(0.006)
        assert c.output == pytest.approx(1.20)
        assert c.total == pytest.approx(1.506)

    def test_offpeak_is_half_of_peak(self):
        kw = dict(miss_tokens=123_456, hit_tokens=789_012, output_tokens=34_567)
        peak = compute_cost(**kw, ts=_utc(2026, 9, 21, 2))
        off = compute_cost(**kw, ts=_utc(2026, 9, 21, 15))
        assert off.total == pytest.approx(peak.total / 2)

    def test_unpriced_when_no_table(self):
        c = compute_cost(
            miss_tokens=1000, hit_tokens=1000, output_tokens=1000,
            ts=_utc(2020, 1, 1, 2),
        )
        assert c.priced is False
        assert c.total == 0.0

    def test_zero_tokens(self):
        c = compute_cost(
            miss_tokens=0, hit_tokens=0, output_tokens=0,
            ts=_utc(2026, 9, 21, 2),
        )
        assert c.total == 0.0
        assert c.priced is True  # 有价目，只是没用量

    def test_saved_if_offpeak(self):
        """峰时记录能省一半；谷时记录省 0。"""
        kw = dict(miss_tokens=1_000_000, hit_tokens=0, output_tokens=0)
        peak = compute_cost(**kw, ts=_utc(2026, 9, 21, 2))
        off = compute_cost(**kw, ts=_utc(2026, 9, 21, 15))
        assert peak.saved_if_offpeak == pytest.approx(peak.total / 2)
        assert off.saved_if_offpeak == 0.0
        # 峰时省下的恰好等于谷时该付的
        assert peak.saved_if_offpeak == pytest.approx(off.total)

    def test_legacy_model_name_is_priced(self):
        """jsonl 里存的是退役名，必须能算出钱来而不是 unpriced。"""
        c = compute_cost(
            miss_tokens=1_000_000, hit_tokens=0, output_tokens=0,
            ts=_utc(2026, 9, 21, 2),
            model="deepseek-v4-flash",
        )
        assert c.priced is True
        assert c.price_model == "deepseek-flash"

    def test_hit_is_fifty_times_cheaper_than_miss(self):
        """命中价 $0.006 对未命中 $0.30 —— 1/50。这个比例是页面结论的基础。"""
        c = compute_cost(
            miss_tokens=1_000_000, hit_tokens=1_000_000, output_tokens=0,
            ts=_utc(2026, 9, 21, 2),
        )
        assert c.miss / c.hit == pytest.approx(50.0)


# ── 窗口切换（页面角标）──


class TestNextWindowChange:
    def test_from_offpeak_to_peak(self):
        cur, nxt = next_window_change(_utc(2026, 9, 21, 0, 30))
        assert cur is False
        assert nxt == _utc(2026, 9, 21, 1)

    def test_from_peak_to_offpeak(self):
        cur, nxt = next_window_change(_utc(2026, 9, 21, 2, 15))
        assert cur is True
        assert nxt == _utc(2026, 9, 21, 4)

    def test_gap_between_two_peak_windows(self):
        """05:00 是两个峰窗之间的谷缝，下一次切换是 06:00。"""
        cur, nxt = next_window_change(_utc(2026, 9, 21, 5, 10))
        assert cur is False
        assert nxt == _utc(2026, 9, 21, 6)

    def test_weekend_spans_to_monday(self):
        """周末整段谷时，下一次切换要跨到周一 01:00。"""
        cur, nxt = next_window_change(_utc(2026, 9, 26, 12))  # 周六
        assert cur is False
        assert nxt == _utc(2026, 9, 28, 1)  # 周一

    def test_friday_evening_spans_weekend(self):
        cur, nxt = next_window_change(_utc(2026, 9, 25, 20))  # 周五 20:00
        assert cur is False
        assert nxt == _utc(2026, 9, 28, 1)

    def test_naive_rejected(self):
        with pytest.raises(ValueError, match="tz-aware"):
            next_window_change(dt.datetime(2026, 9, 21, 2))


# ── 回归：用真实观测值锁住整体口径 ──


def test_real_world_sample_matches_hand_computed():
    """取 2026-09-24 那局 RP 的首次调用，手算核对。

    德国 17:23 CEST = UTC 15:23 → 谷时，单价取峰价的一半：
    miss $0.15/M、hit $0.003/M、output $0.60/M。

      15507 × 0.15/1e6 = 0.00232605
        313 × 0.60/1e6 = 0.00018780
                  合计 = 0.00251385
    """
    c = compute_cost(
        miss_tokens=15_507,
        hit_tokens=0,
        output_tokens=313,
        ts=_utc(2026, 9, 24, 15, 23),
    )
    assert c.peak is False
    assert c.miss == pytest.approx(15_507 * 0.15 / 1_000_000)
    assert c.output == pytest.approx(313 * 0.60 / 1_000_000)
    assert c.total == pytest.approx(0.00251385, abs=1e-8)
