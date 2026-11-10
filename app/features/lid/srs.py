"""复习调度：压缩版 Leitner（纯函数，单测见 tests/test_lid.py）。

等级 0 = 新题，1/2/3 = 复习中，4 = 已掌握。到达等级 L 时把到期日设为「今天 + INTERVAL[L]」：

    新题首次答对      → 1，+1 天
    到期答对 1 → 2    +3 天
    到期答对 2 → 3    +7 天
    到期答对 3 → 4    已掌握（无到期日）

即从第一次答对算起 1 + 3 + 7 = 11 天掌握一题。

- 答错（任何等级）→ 等级 1、到期日 = 今天、streak 归零、进错题本。
  答错后本轮里会再出一次（前端负责）；那次答对就是「到期答对」，照常 1 → 2（+3 天）。
- 提前作答（还没到期就答对）只计入统计和 streak，不升级 —— 随手刷题不打乱间隔。
- promote=False（模拟考）：答对一律按提前作答处理；答错照常退回 1。
- 错题本：答错时进入，streak >= 2 自动移出（也可以手动移出，见 service）。

日期都是 `lid_timezone`（默认德国）的日历日，存成 'YYYY-MM-DD' 字符串，直接字典序比较。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

INTERVAL = {1: 1, 2: 3, 3: 7}
MASTERED = 4
DAYS_TO_MASTER = sum(INTERVAL.values())   # 11
LEAVE_WRONGBOOK_STREAK = 2


def local_today(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def new_state(qid: str) -> dict:
    return {"qid": qid, "level": 0, "due": None, "streak": 0, "seen": 0, "correct": 0, "wrong": 0,
            "last_at": None, "wrongbook": False, "starred": False, "intro_on": None}


def apply_answer(state: Optional[dict], correct: bool, now: datetime, tz: str = "Europe/Berlin",
                 promote: bool = True) -> dict:
    """返回新状态（不改入参）。`state` 为 None 视为新题。"""
    s = dict(state) if state else new_state("")
    today = local_today(now, tz)
    s["seen"] = s.get("seen", 0) + 1
    s["last_at"] = now
    level = s.get("level", 0)

    if not correct:
        s["wrong"] = s.get("wrong", 0) + 1
        s["streak"] = 0
        s["wrongbook"] = True
        s["level"] = 1
        s["due"] = today.isoformat()
        if not s.get("intro_on"):
            s["intro_on"] = today.isoformat()
        return s

    s["correct"] = s.get("correct", 0) + 1
    s["streak"] = s.get("streak", 0) + 1
    if s["streak"] >= LEAVE_WRONGBOOK_STREAK:
        s["wrongbook"] = False
    if not promote:
        return s

    due = s.get("due")
    if level == 0:
        _set_level(s, 1, today)
        if not s.get("intro_on"):
            s["intro_on"] = today.isoformat()
    elif level >= MASTERED:
        pass
    elif due is not None and due > today.isoformat():
        pass                                    # 提前作答：不升级
    else:
        _set_level(s, level + 1, today)
    return s


def _set_level(s: dict, level: int, today: date) -> None:
    s["level"] = level
    s["due"] = None if level >= MASTERED else (today + timedelta(days=INTERVAL[level])).isoformat()


def remaining_days_to_master(s: dict, today: date) -> int:
    """按当前状态、此后每次到期都答对，还要几天掌握（从今天算）。"""
    level = s.get("level", 0)
    if level >= MASTERED:
        return 0
    if level == 0:
        return DAYS_TO_MASTER
    due = date.fromisoformat(s["due"]) if s.get("due") else today
    start = max(0, (due - today).days)
    return start + sum(INTERVAL[k] for k in range(level + 1, MASTERED))


def predict_mastery(states: Iterable[dict], total: int, today: date, daily_new: int,
                    new_left_today: int) -> dict:
    """全部掌握的最早日期：剩余新题按每日上限引入（今天用今日余量），每题再加 11 天；
    已在复习中的题按各自到期日推算。`states` 只含有进度记录的题，缺的按新题算。"""
    states = list(states)
    tracked = {s["qid"] for s in states}
    new_count = (total - len(tracked)) + sum(1 for s in states if s.get("level", 0) == 0)
    worst = 0
    for s in states:
        if s.get("level", 0) > 0:
            worst = max(worst, remaining_days_to_master(s, today))
    intro_last = None
    if new_count > 0:
        daily_new = max(1, daily_new)
        if new_count <= new_left_today:
            intro_last = 0
        else:
            intro_last = math.ceil((new_count - new_left_today) / daily_new)
        worst = max(worst, intro_last + DAYS_TO_MASTER)
    return {"date": (today + timedelta(days=worst)).isoformat(), "days": worst,
            "new_remaining": new_count, "intro_done": None if intro_last is None
            else (today + timedelta(days=intro_last)).isoformat()}
