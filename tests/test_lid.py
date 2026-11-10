"""入籍考试刷题：复习规则（apply_answer）、模拟考抽题与判分、题库文件校验。不碰 DB。"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.features.lid.exam import N_GENERAL, N_STATE, PASS_MARK, pick_exam, score_exam
from app.features.lid.srs import DAYS_TO_MASTER, apply_answer, new_state, predict_mastery

TZ = "Europe/Berlin"
T0 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)   # 柏林 10:00


def at(days: int, hour: int = 9) -> datetime:
    return T0 + timedelta(days=days, hours=hour - 9)


def d(days: int) -> str:
    return (date(2026, 11, 10) + timedelta(days=days)).isoformat()


# ── apply_answer ──


def test_new_correct_goes_to_level1_due_tomorrow():
    s = apply_answer(new_state("1"), True, at(0), TZ)
    assert (s["level"], s["due"], s["streak"], s["seen"], s["correct"], s["wrong"]) == (1, d(1), 1, 1, 1, 0)
    assert s["wrongbook"] is False and s["intro_on"] == d(0)


def test_new_wrong_goes_to_level1_due_today_and_wrongbook():
    s = apply_answer(new_state("1"), False, at(0), TZ)
    assert (s["level"], s["due"], s["streak"], s["wrong"]) == (1, d(0), 0, 1)
    assert s["wrongbook"] is True and s["intro_on"] == d(0)


def test_due_correct_climbs_to_mastered_in_11_days():
    s = apply_answer(new_state("1"), True, at(0), TZ)          # 1, due +1
    s = apply_answer(s, True, at(1), TZ)
    assert (s["level"], s["due"]) == (2, d(4))                 # +3
    s = apply_answer(s, True, at(4), TZ)
    assert (s["level"], s["due"]) == (3, d(11))                # +7
    s = apply_answer(s, True, at(11), TZ)
    assert (s["level"], s["due"]) == (4, None)
    assert DAYS_TO_MASTER == 11 and s["streak"] == 4


def test_overdue_correct_still_promotes():
    s = apply_answer(new_state("1"), True, at(0), TZ)
    s = apply_answer(s, True, at(5), TZ)                       # 逾期 4 天
    assert (s["level"], s["due"]) == (2, d(8))


def test_early_correct_counts_but_does_not_promote():
    s = apply_answer(new_state("1"), True, at(0), TZ)
    s = apply_answer(s, True, at(1), TZ)                       # 2, due d(4)
    e = apply_answer(s, True, at(2), TZ)                       # 提前
    assert (e["level"], e["due"]) == (2, d(4))
    assert e["streak"] == s["streak"] + 1 and e["correct"] == s["correct"] + 1 and e["seen"] == s["seen"] + 1
    # 同一天第二次答对（新题已升 1、明天才到期）也是提前
    s1 = apply_answer(new_state("1"), True, at(0), TZ)
    s2 = apply_answer(s1, True, at(0, 20), TZ)
    assert (s2["level"], s2["due"]) == (1, d(1))


def test_wrong_at_any_level_resets_to_level1_today():
    s = new_state("1")
    for day in (0, 1, 4):
        s = apply_answer(s, True, at(day), TZ)
    assert s["level"] == 3
    w = apply_answer(s, False, at(6), TZ)
    assert (w["level"], w["due"], w["streak"], w["wrongbook"]) == (1, d(6), 0, True)
    assert w["intro_on"] == d(0)                               # 引入日不变（不占今日新题配额）
    m = dict(s, level=4, due=None)
    w = apply_answer(m, False, at(20), TZ)
    assert (w["level"], w["due"]) == (1, d(20))


def test_requeue_after_wrong_relearns_at_level1():
    """答错后本轮里重出那次答对：留在 1 级、明天再考，不跳到 2 级。"""
    w = apply_answer(new_state("1"), False, at(0), TZ)
    assert w["relearn"] is True
    s = apply_answer(w, True, at(0, 10), TZ)
    assert (s["level"], s["due"], s["streak"], s["relearn"]) == (1, d(1), 1, False)
    # 之后照常 1 → 2 → 3 → 4，从重新学那次答对起 11 天掌握
    s = apply_answer(s, True, at(1), TZ)
    assert (s["level"], s["due"]) == (2, d(4))
    s = apply_answer(s, True, at(4), TZ)
    s = apply_answer(s, True, at(11), TZ)
    assert s["level"] == 4


def test_wrong_question_never_scheduled_later_than_first_time_correct():
    ok = apply_answer(new_state("1"), True, at(0), TZ)
    w = apply_answer(apply_answer(new_state("2"), False, at(0), TZ), True, at(0, 10), TZ)
    assert w["due"] <= ok["due"]


def test_relearn_survives_early_and_exam_answers():
    w = apply_answer(apply_answer(new_state("1"), False, at(0), TZ), True, at(0, 10), TZ)   # 1, due d(1)
    e = apply_answer(w, True, at(0, 20), TZ)                                                # 提前作答
    assert (e["level"], e["due"]) == (1, d(1))
    w2 = apply_answer(new_state("1"), False, at(0), TZ)
    e2 = apply_answer(w2, True, at(0, 10), TZ, promote=False)                               # 模拟考答对
    assert e2["relearn"] is True and e2["level"] == 1
    s = apply_answer(e2, True, at(0, 12), TZ)                                               # 之后练习仍是重新学
    assert (s["level"], s["due"], s["relearn"]) == (1, d(1), False)


def test_wrongbook_leaves_at_streak_2():
    s = apply_answer(new_state("1"), False, at(0), TZ)
    s = apply_answer(s, True, at(0, 10), TZ)
    assert s["wrongbook"] is True and s["streak"] == 1
    s = apply_answer(s, True, at(1), TZ)
    assert s["wrongbook"] is False and s["streak"] == 2 and s["level"] == 2
    s = apply_answer(s, False, at(2), TZ)
    assert s["wrongbook"] is True


def test_exam_mode_never_promotes_but_demotes():
    s = apply_answer(new_state("1"), True, at(0), TZ, promote=False)
    assert s["level"] == 0 and s["due"] is None and s["streak"] == 1 and s["correct"] == 1
    s = apply_answer(s, False, at(0), TZ, promote=False)
    assert (s["level"], s["due"], s["wrongbook"]) == (1, d(0), True)


def test_apply_answer_does_not_mutate_input():
    s = new_state("1")
    before = dict(s)
    apply_answer(s, True, at(0), TZ)
    assert s == before


def test_day_boundary_uses_berlin_calendar():
    late = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc)   # 柏林已是 11-11 00:30
    s = apply_answer(new_state("1"), True, late, TZ)
    assert s["due"] == "2026-11-12"


def test_predict_mastery():
    today = date(2026, 11, 10)
    p = predict_mastery([], 310, today, 55, 55)
    # 310 新题：今天 55，余 255 → 再 5 天（ceil(255/55)=5），最后一批第 5 天引入 + 11
    assert p["days"] == 5 + 11 and p["new_remaining"] == 310
    s = apply_answer(new_state("1"), True, at(0), TZ)
    s["qid"] = "1"
    p = predict_mastery([s], 1, today, 55, 54)
    assert p["days"] == 1 + 3 + 7 and p["new_remaining"] == 0
    w = apply_answer(new_state("2"), False, at(0), TZ)   # 刚答错：今天重新学 → +1 → 2 级 +3 → 3 级 +7
    w["qid"] = "2"
    assert predict_mastery([w], 1, today, 55, 54)["days"] == 1 + 3 + 7


# ── 模拟考 ──

GEN = [str(i) for i in range(1, 301)]
HE = [f"HE-{i}" for i in range(1, 11)]


@pytest.mark.parametrize("seed", range(20))
def test_pick_exam_30_plus_3_unique(seed):
    qs = pick_exam(GEN, HE, random.Random(seed))
    assert len(qs) == N_GENERAL + N_STATE == 33
    assert len(set(qs)) == 33
    assert all(q in GEN for q in qs[:30])
    assert all(q.startswith("HE-") for q in qs[30:])
    assert sum(q.startswith("HE-") for q in qs) == 3


def test_pick_exam_rejects_small_bank():
    with pytest.raises(ValueError):
        pick_exam(GEN[:10], HE)


def test_score_exam():
    qids = [str(i) for i in range(1, 34)]
    key = {q: 0 for q in qids}
    chosen = [0] * 17 + [1] * 15 + [None]
    r = score_exam(qids, chosen, key)
    assert r["score"] == 17 and r["passed"] is True and PASS_MARK == 17
    assert r["items"][-1]["correct"] is False
    r = score_exam(qids, [0] * 16 + [None] * 17, key)
    assert r["score"] == 16 and r["passed"] is False
    with pytest.raises(ValueError):
        score_exam(qids, [0], key)


# ── 题库文件 ──


@pytest.fixture(scope="module")
def bank():
    from app.features.lid.bank import BANK_FILE

    return json.loads(BANK_FILE.read_text("utf-8"))


def test_bank_ids_and_answers(bank):
    qs = bank["questions"]
    assert len(qs) == 310
    assert [q["id"] for q in qs] == [str(i) for i in range(1, 301)] + [f"HE-{i}" for i in range(1, 11)]
    for q in qs:
        assert q["answer"] in (0, 1, 2, 3), q["id"]
        assert len(q["de"]["options"]) == 4 and len(q["zh"]["options"]) == 4, q["id"]
        assert all(q["de"]["options"]) and all(o.strip() for o in q["zh"]["options"]), q["id"]
        assert q["zh"]["q"].strip() and q["exp"]["de"].strip() and q["exp"]["zh"].strip(), q["id"]
        assert not q["exp"]["de"].lower().startswith("die richtige antwort ist"), q["id"]
        assert q["part"] == ("state" if q["id"].startswith("HE-") else "general")
    assert bank["meta"]["state"] == "HE" and bank["meta"]["stand"] == "07.05.2025"


def test_bank_anchor_answers(bank):
    by = {q["id"]: q for q in bank["questions"]}
    assert by["1"]["de"]["options"][by["1"]["answer"]] == "hier Meinungsfreiheit gilt."
    assert by["14"]["de"]["options"][by["14"]["answer"]] == "meine Meinung im Internet äußern kann."


def test_bank_images_exist(bank):
    from app.features.lid.bank import IMG_DIR, image_path

    with_img = [q for q in bank["questions"] if q["images"]]
    assert sorted(q["id"] for q in with_img) == sorted(
        ["21", "55", "70", "130", "176", "181", "187", "209", "216", "226", "235", "HE-1", "HE-8"])
    for q in with_img:
        for name in q["images"]:
            assert image_path(name) is not None, name
    on_disk = {p.name for p in IMG_DIR.glob("*")}
    assert on_disk == {n for q in with_img for n in q["images"]}


def test_image_name_whitelist():
    from app.features.lid.bank import image_path

    for bad in ("../questions.json", "21.png/../../x", "a.png", "21.PNG", "HE-1.png.bak", "%2e%2e"):
        assert image_path(bad) is None, bad
