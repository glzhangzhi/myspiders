"""计时：操作日志重放的规则（app/features/timelog/replay.py）。

这里钉住的每一条都对应一种「离线 + 多端」下会真实发生、且出错时不报错的情况：
重复同步、乱序到达、取消后被晚到的操作救活、网页改时间段写坏数据。
"""

from __future__ import annotations

import itertools
import random

import pytest

from app.features.timelog import replay as R

T0 = 1_800_000_000_000          # 2027-01-15 附近
M = 60_000
_ids = itertools.count()


def op(kind, ts, rid="rec-00000001", data=None, **kw):
    o = {"id": f"op-{next(_ids):08d}", "ts": ts, "kind": kind, "rid": rid, **kw}
    if data is not None:
        o["data"] = data
    return o


def basic_ops():
    return [
        op("start", T0, data={"name": "写接口", "estimate": 30, "estimate_source": "guess"}),
        op("pause", T0 + 10 * M),
        op("resume", T0 + 15 * M),
        op("end", T0 + 40 * M),
    ]


# ─────────────────────── 基本重放 ───────────────────────

def test_actual_excludes_pauses():
    rec = R.replay_record(basic_ops())
    assert rec["status"] == "done"
    assert rec["segments"] == [{"s": T0, "e": T0 + 10 * M}, {"s": T0 + 15 * M, "e": T0 + 40 * M}]
    assert R.derived(rec, T0 + 99 * M)["actual_ms"] == 35 * M


def test_running_counts_to_now():
    rec = R.replay_record([op("start", T0)])
    assert rec["status"] == "running"
    assert rec["name"] is None                       # 立即开始 = 未命名
    assert R.derived(rec, T0 + 7 * M)["actual_ms"] == 7 * M


def test_end_while_paused_keeps_pause_time_out():
    rec = R.replay_record([op("start", T0), op("pause", T0 + 5 * M), op("end", T0 + 60 * M)])
    assert rec["status"] == "done"
    assert R.derived(rec, T0 + 99 * M)["actual_ms"] == 5 * M


# ─────────────────────── 离线同步 ───────────────────────

def test_any_arrival_order_gives_same_result():
    """乱序到达：手机离线时攒的操作，与网页操作交错着到服务器。"""
    ops = basic_ops() + [op("edit", T0 + 50 * M, data={"note": "比预估慢"})]
    want = R.replay_record(ops)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = ops[:]
        rng.shuffle(shuffled)
        assert R.replay_record(shuffled) == want


def test_ops_before_start_wait_for_it():
    """pause 先到、start 还在路上：记录暂时不存在，而不是凭空造一条。"""
    ops = basic_ops()
    assert R.replay_record(ops[1:]) is None
    assert R.replay_record(ops)["status"] == "done"


def test_duplicate_state_ops_are_noops():
    """重复点击（或重复同步出两个不同 id 的同一动作）不改变结果，也不算冲突。"""
    ops = basic_ops()
    ops.insert(2, op("pause", T0 + 11 * M))
    ops.insert(4, op("resume", T0 + 16 * M))
    rec = R.replay_record(ops)
    assert rec["segments"] == [{"s": T0, "e": T0 + 10 * M}, {"s": T0 + 15 * M, "e": T0 + 40 * M}]
    assert rec["conflicts"] == []


def test_second_start_with_same_rid_ignored():
    rec = R.replay_record([op("start", T0, data={"name": "A"}), op("start", T0 + M, data={"name": "B"})])
    assert rec["name"] == "A"
    assert rec["segments"] == [{"s": T0, "e": None}]


# ─────────────────────── 取消 = 墓碑 ───────────────────────

def test_cancel_is_final_even_if_start_arrives_late():
    """取消后，晚到的 start（重复同步）不能让记录复活。"""
    start = op("start", T0)
    cancel = op("cancel", T0 + 3 * M)
    assert R.replay_record([cancel, start])["status"] == "cancelled"
    resume = op("resume", T0 + 5 * M)
    assert R.replay_record([start, cancel, resume])["status"] == "cancelled"


def test_cancelled_while_running_has_no_stored_actual():
    """计时中被取消：最后一段仍开放。落库算耗时时不能拿 None 去减（实测踩过：整批同步 500）。"""
    rec = R.replay_record([op("start", T0), op("cancel", T0 + 3 * M)])
    assert rec["segments"][-1]["e"] is None
    assert R.closed_actual_ms(rec) is None
    assert R.closed_actual_ms(R.replay_record(basic_ops())) == 35 * M


def test_delete_is_final():
    ops = basic_ops() + [op("delete", T0 + 60 * M), op("edit", T0 + 61 * M, data={"name": "x"})]
    rec = R.replay_record(ops)
    assert rec["status"] == "deleted"
    assert rec["name"] == "写接口"


# ─────────────────────── 冲突 ───────────────────────

def test_pause_before_segment_start_is_conflict():
    """网页把开始时间往后改了，手机上更早的暂停就落在段外：标冲突，不写坏数据。"""
    rec = R.replay_record([
        op("start", T0),
        op("set_segments", T0 + M, data={"segments": [{"s": T0 + 20 * M, "e": None}]}, seq=2),
        op("pause", T0 + 10 * M),
    ])
    # 按 ts 重放：set_segments(T0+1) 先把开始改到 T0+20，pause(T0+10) 随后落在段外
    assert rec["status"] == "running"
    assert rec["conflicts"][0]["kind"] == "pause"


def test_invalid_set_segments_is_conflict_not_crash():
    ops = basic_ops() + [op("set_segments", T0 + 50 * M,
                            data={"segments": [{"s": T0 + 5 * M, "e": T0}]})]
    rec = R.replay_record(ops)
    assert rec["status"] == "done"
    assert len(rec["segments"]) == 2                 # 没被写坏
    assert "改时间段未生效" in rec["conflicts"][0]["reason"]


# ─────────────────────── 网页：补记 / 改时间段 ───────────────────────

def test_create_backfill():
    rec = R.replay_record([op("create", T0 + 99 * M, data={
        "name": "打游戏", "segments": [{"s": T0, "e": T0 + 30 * M}]})])
    assert rec["status"] == "done"
    assert R.derived(rec, T0 + 200 * M) == {"actual_ms": 30 * M, "span_start": T0, "span_end": T0 + 30 * M}


def test_set_segments_on_running_keeps_open_tail():
    rec = R.replay_record([
        op("start", T0),
        op("set_segments", T0 + 5 * M, data={"segments": [{"s": T0 - 10 * M, "e": None}]}),
    ])
    assert rec["status"] == "running"
    assert rec["segments"] == [{"s": T0 - 10 * M, "e": None}]


def test_later_pause_after_edit_still_applies():
    rec = R.replay_record([
        op("start", T0),
        op("set_segments", T0 + 5 * M, data={"segments": [{"s": T0 - 10 * M, "e": None}]}),
        op("pause", T0 + 20 * M),
    ])
    assert rec["segments"] == [{"s": T0 - 10 * M, "e": T0 + 20 * M}]


def test_edit_cannot_touch_button_id():
    assert R.validate_op(op("edit", T0, data={"button_id": "b"}), T0) is not None


@pytest.mark.parametrize("segs,open_tail,msg", [
    ([], False, "非空"),
    ([{"s": 5, "e": 5}], False, "早于结束"),
    ([{"s": 0, "e": 10}, {"s": 5, "e": 20}], False, "互不重叠"),
    ([{"s": 0, "e": None}], False, "最后一段才能"),
    ([{"s": 0, "e": None}, {"s": 5, "e": 6}], True, "最后一段才能"),
    ([{"s": 0, "e": 10}], True, "不能有结束"),
])
def test_validate_segments_rejects(segs, open_tail, msg):
    with pytest.raises(R.SegmentError, match=msg):
        R.validate_segments(segs, open_tail=open_tail)


def test_validate_segments_rejects_future():
    with pytest.raises(R.SegmentError, match="晚于现在"):
        R.validate_segments([{"s": T0, "e": T0 + 99 * M}], open_tail=False, now_ms=T0)


def test_touching_segments_ok():
    R.validate_segments([{"s": 0, "e": 10}, {"s": 10, "e": 20}], open_tail=False)


# ─────────────────────── 拆分 / 合并 ───────────────────────

def test_split_inside_segment():
    left, right = R.split_segments([{"s": 0, "e": 45}], 20)
    assert left == [{"s": 0, "e": 20}] and right == [{"s": 20, "e": 45}]


def test_split_in_pause_gap():
    left, right = R.split_segments([{"s": 0, "e": 10}, {"s": 20, "e": 30}], 15)
    assert left == [{"s": 0, "e": 10}] and right == [{"s": 20, "e": 30}]


@pytest.mark.parametrize("at", [0, 45, 99])
def test_split_outside_rejected(at):
    with pytest.raises(R.SegmentError):
        R.split_segments([{"s": 0, "e": 45}], at)


def test_split_running_rejected():
    with pytest.raises(R.SegmentError, match="计时中"):
        R.split_segments([{"s": 0, "e": None}], 5)


def test_coalesce_overlap_counts_once():
    assert R.coalesce([{"s": 20, "e": 40}, {"s": 0, "e": 10}, {"s": 30, "e": 50}, {"s": 50, "e": 60}]) \
        == [{"s": 0, "e": 10}, {"s": 20, "e": 60}]


# ─────────────────────── 操作形状 ───────────────────────

@pytest.mark.parametrize("bad,why", [
    ({"kind": "explode"}, "不认识的操作"),
    ({"ts": "123"}, "毫秒整数"),
    ({"ts": True}, "毫秒整数"),
    ({"ts": 1000}, "合理范围"),
    ({"ts": T0 + 2 * 86_400_000}, "合理范围"),
    ({"id": "x"}, "id"),
    ({"kind": "pause", "data": {"x": 1}}, "不带 data"),
    ({"kind": "start", "data": {"estimate": 0}}, "estimate"),
    ({"kind": "start", "data": {"estimate": 30.5}}, "estimate"),
    ({"kind": "start", "data": {"estimate_source": "auto"}}, "estimate_source"),
    ({"kind": "start", "data": {"color": "red"}}, "不认识的字段"),
    ({"kind": "edit", "data": {}}, "没有任何字段"),
    ({"kind": "set_segments", "data": {"segments": [{"s": 1}], "x": 1}}, "只带 segments"),
    ({"kind": "button.put", "data": {"name": " "}}, "名称"),
])
def test_validate_op_rejects(bad, why):
    o = {"id": "op-aaaaaaaa", "ts": T0, "kind": "start", "rid": "rec-00000001", **bad}
    assert why in (R.validate_op(o, T0) or "")


def test_validate_op_accepts_phone_ops():
    for o in basic_ops() + [op("cancel", T0), op("edit", T0, data={"estimate": 45, "estimate_source": "guess"})]:
        assert R.validate_op(o, T0 + 60 * M) is None, o


# ─────────────────────── 按钮 ───────────────────────

def test_button_last_put_wins_and_delete_is_final():
    b = "btn-0000001"
    p1 = op("button.put", T0, rid=b, data={"name": "拖地", "initial_estimate": 20})
    p2 = op("button.put", T0 + M, rid=b, data={"name": "拖地板", "guess_first": True})
    assert R.replay_button([p2, p1])["name"] == "拖地板"
    d = op("button.delete", T0 + 2 * M, rid=b)
    late = op("button.put", T0 + 3 * M, rid=b, data={"name": "复活"})
    assert R.replay_button([p1, p2, d, late])["deleted"] is True


# ─────────────────────── 参考时长 ───────────────────────

@pytest.mark.parametrize("xs,want", [
    ([], {"kind": "none", "n": 0, "lo": None, "hi": None}),
    ([28], {"kind": "last", "n": 1, "lo": 30, "hi": 30}),
    ([12, 15, 18], {"kind": "range", "n": 3, "lo": 10, "hi": 20}),
    ([18, 22, 25, 20, 30, 24], {"kind": "iqr", "n": 6, "lo": 20, "hi": 25}),
    ([3.4, 7.6], {"kind": "range", "n": 2, "lo": 3, "hi": 8}),
])
def test_reference(xs, want):
    assert R.reference(xs) == want


@pytest.mark.parametrize("m,want", [(2.5, 3), (0.2, 1), (12.5, 15), (17.5, 20), (22.4, 20)])
def test_round_ref_half_up_like_js(m, want):
    """与手机端 Math.round 一致：.5 一律进位（Python 的 round 是银行家舍入）。"""
    assert R.round_ref(m) == want


def test_reference_iqr_ignores_outlier():
    """一次 120 分的异常（忘了暂停）拉不动「通常」区间。"""
    assert R.reference([35, 50, 42, 65, 40, 55, 120])["hi"] <= 60
