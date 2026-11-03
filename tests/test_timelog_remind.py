"""计时提醒规则（app/features/timelog/remind.py）。

最容易出错又不报错的是时间：跨午夜的静音、周末、夏令时、静音结束后空闲从哪算起。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.features.timelog import remind as T

BER = ZoneInfo("Europe/Berlin")
M = 60_000
CFG = {
    "timezone": "Europe/Berlin", "quiet_start": "20:00", "quiet_end": "08:00", "quiet_weekends": True,
    "idle_remind_minutes": 30, "overtime_ratio": 1.5, "overtime_cap_minutes": 180, "muted_until": 0,
}


def at(y, mo, d, h, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=BER).timestamp() * 1000)


# 2027-01-13 是周三
WED = (2027, 1, 13)


@pytest.mark.parametrize("h,mi,quiet", [(7, 59, True), (8, 0, False), (19, 59, False), (20, 0, True), (23, 0, True), (2, 0, True)])
def test_quiet_crosses_midnight(h, mi, quiet):
    assert T.is_quiet(at(*WED, h, mi), CFG) is quiet


def test_weekend_quiet_all_day():
    assert T.is_quiet(at(2027, 1, 16, 12), CFG)          # 周六中午
    assert not T.is_quiet(at(2027, 1, 16, 12), {**CFG, "quiet_weekends": False})


def test_same_start_end_means_no_quiet():
    assert not T.is_quiet(at(*WED, 23), {**CFG, "quiet_start": "00:00", "quiet_end": "00:00"})


def run(rec_status="paused", **kw):
    return {"id": "r1", "status": rec_status, "estimate": None, "name": "写接口", **kw}


def test_idle_fires_after_threshold():
    now = at(*WED, 10, 0)
    assert T.evaluate(now, CFG, [run(actual_ms=5 * M)], now - 29 * M) == []
    items = T.evaluate(now, CFG, [], now - 31 * M)
    assert [i["kind"] for i in items] == ["idle"]
    assert items[0]["key"] == f"idle:{now - 31 * M}"


def test_idle_counts_from_quiet_exit():
    """前一晚 19:00 起就没记，早上 8:05 不该立刻推「空闲 13 小时」，要等到 8:30。"""
    last = at(2027, 1, 12, 19, 0)
    assert T.evaluate(at(*WED, 8, 5), CFG, [], last) == []
    items = T.evaluate(at(*WED, 8, 31), CFG, [], last)
    assert items and items[0]["minutes"] == 31
    # 去重键仍是真实的空闲起点：同一段空闲只推一次
    assert items[0]["key"] == f"idle:{last}"


def test_idle_after_weekend_counts_from_monday_morning():
    last = at(2027, 1, 15, 17, 0)                      # 周五 17:00
    assert T.last_quiet_exit(at(2027, 1, 18, 8, 10), last, CFG) == at(2027, 1, 18, 8, 0)
    assert T.evaluate(at(2027, 1, 18, 8, 10), CFG, [], last) == []


def test_never_used_no_idle():
    assert T.evaluate(at(*WED, 10), CFG, [], None) == []


def test_running_suppresses_idle():
    now = at(*WED, 10)
    assert T.evaluate(now, CFG, [run("running", actual_ms=5 * M)], now - 99 * M) == []


def test_overtime_with_estimate_uses_ratio():
    now = at(*WED, 10)
    r = run("running", estimate=30, actual_ms=44 * M)
    assert T.evaluate(now, CFG, [r], None) == []
    r["actual_ms"] = 45 * M
    items = T.evaluate(now, CFG, [r], None)
    assert items[0]["kind"] == "overtime" and items[0]["key"] == "overtime:r1"


def test_overtime_without_estimate_uses_cap():
    now = at(*WED, 10)
    assert T.evaluate(now, CFG, [run("running", actual_ms=179 * M)], None) == []
    assert T.evaluate(now, CFG, [run("running", actual_ms=180 * M)], None)


def test_paused_never_overtime():
    assert T.evaluate(at(*WED, 10), CFG, [run("paused", actual_ms=999 * M)], at(*WED, 9, 50)) == []


def test_quiet_and_mute_silence_everything():
    r = run("running", actual_ms=999 * M)
    assert T.evaluate(at(*WED, 21), CFG, [r], None) == []
    now = at(*WED, 10)
    assert T.evaluate(now, {**CFG, "muted_until": now + 1}, [r], None) == []


def test_end_of_local_day_handles_dst():
    # 2027-03-28 是夏令时开始（那天只有 23 小时）
    assert T.end_of_local_day(at(2027, 3, 28, 10), "Europe/Berlin") == at(2027, 3, 29, 0)
    assert T.end_of_local_day(at(2027, 3, 28, 0, 30), "Europe/Berlin") - at(2027, 3, 28, 0) == 23 * 60 * M


def test_messages_are_neutral():
    for item in (
        {"kind": "idle", "minutes": 45},
        {"kind": "overtime", "minutes": 95, "record": run("running", estimate=30)},
        {"kind": "overtime", "minutes": 200, "record": run("running", name=None)},
    ):
        title, body = T.message(item)
        for word in ("超时", "失败", "浪费"):
            assert word not in title + body
