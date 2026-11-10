"""模拟考：抽题与判分（纯函数）。

官方规则：33 题 = 30 道通用题 + 3 道本州题，60 分钟，答对 17 题及格。
"""

from __future__ import annotations

import random
from typing import Optional, Sequence

N_GENERAL = 30
N_STATE = 3
N_TOTAL = N_GENERAL + N_STATE
PASS_MARK = 17
DURATION_MIN = 60


def pick_exam(general_ids: Sequence[str], state_ids: Sequence[str],
              rng: Optional[random.Random] = None) -> list[str]:
    """通用题在前（随机顺序），本州题在后。不重复。"""
    rng = rng or random.SystemRandom()
    if len(set(general_ids)) < N_GENERAL or len(set(state_ids)) < N_STATE:
        raise ValueError("题库题数不足以抽一套模拟考")
    return rng.sample(sorted(set(general_ids)), N_GENERAL) + rng.sample(sorted(set(state_ids)), N_STATE)


def score_exam(qids: Sequence[str], chosen: Sequence[Optional[int]], key: dict[str, int]) -> dict:
    """chosen[i] 是第 i 题所选的官方顺序下标，None = 没答（算错）。"""
    if len(chosen) != len(qids):
        raise ValueError(f"答案数 {len(chosen)} 与题数 {len(qids)} 不符")
    items = []
    for qid, c in zip(qids, chosen):
        ok = c is not None and c == key[qid]
        items.append({"qid": qid, "chosen": c, "answer": key[qid], "correct": ok})
    score = sum(1 for it in items if it["correct"])
    return {"score": score, "total": len(qids), "passed": score >= PASS_MARK, "items": items}
