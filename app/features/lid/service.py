"""入籍考试刷题的业务层：作答、今日概况、统计、模拟考。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.core.settings import settings
from app.features.lid import exam as exam_rules
from app.features.lid.bank import get_bank
from app.features.lid.repo import repo
from app.features.lid.srs import MASTERED, apply_answer, local_today, new_state, predict_mastery

MODES = ("today", "practice", "wrongbook", "starred", "exam")


class LidError(ValueError):
    """请求不合法（→ 400/404）。"""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(now: Optional[datetime] = None) -> date:
    return local_today(now or _now(), settings.lid_timezone)


def _exam_date() -> Optional[date]:
    try:
        return date.fromisoformat(settings.lid_exam_date.strip()) if settings.lid_exam_date.strip() else None
    except ValueError:
        return None


def _public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k != "_id"}
    if isinstance(out.get("last_at"), datetime):
        out["last_at"] = out["last_at"].isoformat()
    return out


def _new_left(progress: list[dict], today: date) -> int:
    introduced = sum(1 for s in progress if s.get("intro_on") == today.isoformat())
    return max(0, settings.lid_daily_new - introduced)


# ── 作答 ──


def answer(qid: str, chosen: int, mode: str) -> dict:
    bank = get_bank()
    q = bank.by_id.get(qid)
    if q is None:
        raise LidError(f"没有这道题：{qid}", 404)
    if chosen not in (0, 1, 2, 3):
        raise LidError("chosen 必须是 0..3")
    if mode not in MODES or mode == "exam":
        raise LidError(f"mode 不合法：{mode}")
    now = _now()
    correct = chosen == q["answer"]
    state = repo.get(qid) or new_state(qid)
    state = apply_answer(state, correct, now, settings.lid_timezone)
    state["qid"] = qid
    repo.save(state)
    repo.log_answers([{"qid": qid, "mode": mode, "chosen": chosen, "correct": correct, "at": now,
                       "day": _today(now).isoformat()}])
    return {"correct": correct, "answer": q["answer"], "state": _public(state)}


def set_star(qid: str, starred: bool) -> dict:
    if qid not in get_bank().by_id:
        raise LidError(f"没有这道题：{qid}", 404)
    return _public(repo.set_flag(qid, "starred", starred))


def remove_from_wrongbook(qid: str) -> dict:
    if qid not in get_bank().by_id:
        raise LidError(f"没有这道题：{qid}", 404)
    return _public(repo.set_flag(qid, "wrongbook", False))


# ── 概况 ──


def state() -> dict:
    bank = get_bank()
    now = _now()
    today = _today(now)
    progress = [s for s in repo.all_progress() if s["qid"] in bank.by_id]
    exam_day = _exam_date()
    return {
        "today": today.isoformat(),
        "progress": {s["qid"]: _public(s) for s in progress},
        "done_today": repo.count_on(today.isoformat()),
        "new_left": _new_left(progress, today),
        "daily_new": settings.lid_daily_new,
        "exam_date": exam_day.isoformat() if exam_day else None,
        "days_left": (exam_day - today).days if exam_day else None,
        "total": len(bank),
        "mastered": sum(1 for s in progress if s.get("level", 0) >= MASTERED),
        "due": sum(1 for s in progress if 0 < s.get("level", 0) < MASTERED and s.get("due") and s["due"] <= today.isoformat()),
        "wrongbook": sum(1 for s in progress if s.get("wrongbook")),
        "starred": sum(1 for s in progress if s.get("starred")),
        "prediction": _prediction(progress, today, exam_day),
    }


def _prediction(progress: list[dict], today: date, exam_day: Optional[date]) -> dict:
    p = predict_mastery(progress, len(get_bank()), today, settings.lid_daily_new, _new_left(progress, today))
    p["on_time"] = None if exam_day is None else date.fromisoformat(p["date"]) <= exam_day
    return p


def stats() -> dict:
    bank = get_bank()
    today = _today()
    progress = [s for s in repo.all_progress() if s["qid"] in bank.by_id]
    levels = [0] * (MASTERED + 1)
    for s in progress:
        levels[min(MASTERED, max(0, s.get("level", 0)))] += 1
    levels[0] += len(bank) - len(progress)
    since = today - timedelta(days=13)
    per_day = repo.per_day(since.isoformat())
    days = []
    for i in range(14):
        d = (since + timedelta(days=i)).isoformat()
        days.append({"day": d, **per_day.get(d, {"n": 0, "ok": 0})})
    totals = repo.totals()
    worst = sorted((s for s in progress if s.get("wrong", 0) > 0),
                   key=lambda s: (-s["wrong"], s.get("correct", 0), bank.order[s["qid"]]))[:10]
    return {
        "levels": levels,
        "days": days,
        "answered": totals["n"],
        "accuracy": (totals["ok"] / totals["n"]) if totals["n"] else None,
        "worst": [{"qid": s["qid"], "wrong": s["wrong"], "correct": s.get("correct", 0)} for s in worst],
        "prediction": _prediction(progress, today, _exam_date()),
        "exam_date": settings.lid_exam_date or None,
    }


# ── 模拟考 ──


def _exam_public(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "qids": doc["qids"],
        "started_at": doc["started_at"].isoformat(),
        "finished_at": doc["finished_at"].isoformat() if doc.get("finished_at") else None,
        "score": doc.get("score"),
        "passed": doc.get("passed"),
        "answers": doc.get("answers"),
    }


def exam_start() -> dict:
    bank = get_bank()
    now = _now()
    repo.delete_unfinished_before(now - timedelta(days=1))   # 放弃的半截考卷不留
    qids = exam_rules.pick_exam(bank.general_ids, bank.state_ids)
    exam_id = repo.create_exam(qids, now)
    return {"id": exam_id, "qids": qids, "started_at": now.isoformat(),
            "deadline": (now + timedelta(minutes=exam_rules.DURATION_MIN)).isoformat(),
            "duration_min": exam_rules.DURATION_MIN, "pass_mark": exam_rules.PASS_MARK}


def exam_finish(exam_id: str, answers: list[Optional[int]]) -> dict:
    bank = get_bank()
    doc = repo.get_exam(exam_id)
    if doc is None:
        raise LidError("没有这场模拟考", 404)
    if doc.get("finished_at"):
        return {**_exam_public(doc), **exam_rules.score_exam(doc["qids"], doc["answers"], bank.key)}
    if any(a is not None and a not in (0, 1, 2, 3) for a in answers):
        raise LidError("答案下标必须是 0..3 或 null")
    try:
        result = exam_rules.score_exam(doc["qids"], answers, bank.key)
    except ValueError as exc:
        raise LidError(str(exc))
    now = _now()
    if not repo.finish_exam(exam_id, list(answers), now, result["score"], result["passed"]):
        doc = repo.get_exam(exam_id)
        return {**_exam_public(doc), **exam_rules.score_exam(doc["qids"], doc["answers"], bank.key)}
    # 每题写进进度：答错 → 等级 1 并进错题本；答对按提前作答（不升级）。没答的题不计。
    day = _today(now).isoformat()
    logs = []
    for it in result["items"]:
        if it["chosen"] is None:
            continue
        s = repo.get(it["qid"]) or new_state(it["qid"])
        s = apply_answer(s, it["correct"], now, settings.lid_timezone, promote=False)
        s["qid"] = it["qid"]
        repo.save(s)
        logs.append({"qid": it["qid"], "mode": "exam", "chosen": it["chosen"], "correct": it["correct"],
                     "at": now, "day": day})
    repo.log_answers(logs)
    doc = repo.get_exam(exam_id)
    return {**_exam_public(doc), **result}


def exams() -> list[dict]:
    out = []
    for d in repo.list_exams():
        out.append({"id": str(d["_id"]), "started_at": d["started_at"].isoformat(),
                    "finished_at": d["finished_at"].isoformat(), "score": d["score"], "passed": d["passed"],
                    "total": len(d["qids"])})
    return out
