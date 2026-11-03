"""计时：空闲 / 超时提醒（需求 3.6、3.12）。

由服务端定时判断、经 ntfy 推送 —— HyperOS 会杀 PWA 后台，手机自己定不了时。

══════════════════════════════════════════════════════════════════════
 规则
══════════════════════════════════════════════════════════════════════

- **空闲**：没有计时中的记录（暂停的不算计时），且距「最后一次动静」超过
  `idle_remind_minutes`。最后一次动静 = 所有记录里最晚的结束/暂停时刻，与手机
  最后一条操作的 ts，取较晚者 —— 后者让「刚点了取消」「刚编辑了按钮」也算动静。
  **一段空闲只提醒一次**（按空闲起点去重）：反复催促正是需求 3.12 要避免的惩罚感。
- **超时**：计时中的记录，实际耗时超过「预估 × overtime_ratio」（有预估时）或
  `overtime_cap_minutes`（没预估时）。**每条记录只提醒一次**。文案问「是不是忘了暂停」，
  不说「超时」。
- **静音**：每天 `quiet_start`–`quiet_end`（可跨午夜）与周末（`quiet_weekends`）。
  静音期间什么都不推。空闲时长**从静音结束才开始算** —— 否则早上 8:00 静音一结束，
  前一晚 19:00 起的「空闲 13 小时」会立刻推出来，那不是提醒，是点名。
- **今天不再提醒**：`muted_until` 之前什么都不推（ntfy 通知上的按钮设成本地次日 0 点）。

已知局限：服务端只知道**已同步**的状态。手机离线时开始的计时它看不到，可能误报
空闲。没有办法在服务端区分「离线」和「真的没在计时」，文案因此写成提问而非断言。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

MIN_MS = 60_000
QUIET_LOOKBACK_MIN = 7 * 24 * 60   # 往回找静音结束点最多看 7 天


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def is_quiet(ms: int, cfg: dict) -> bool:
    """本地时间 ms 是否处于静音时段。"""
    t = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(cfg["timezone"]))
    if cfg.get("quiet_weekends") and t.weekday() >= 5:
        return True
    start, end, cur = _hhmm(cfg["quiet_start"]), _hhmm(cfg["quiet_end"]), t.hour * 60 + t.minute
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end           # 跨午夜，如 20:00–08:00


def last_quiet_exit(now_ms: int, since_ms: int, cfg: dict) -> Optional[int]:
    """(since, now] 内最近一次「静音 → 非静音」的时刻；期间一直不静音返回 None。

    按分钟往回扫。静音边界都是整分钟，所以分钟粒度是精确的；最多 7 天 = 1 万次
    本地时间换算，5 分钟跑一次毫无压力，换来的是不用手写跨午夜/周末/夏令时的区间运算。
    """
    floor = max(since_ms, now_ms - QUIET_LOOKBACK_MIN * MIN_MS)
    t = now_ms - now_ms % MIN_MS
    while t > floor:
        if is_quiet(t - MIN_MS, cfg) and not is_quiet(t, cfg):
            return t
        t -= MIN_MS
    return None


def end_of_local_day(now_ms: int, tz: str) -> int:
    z = ZoneInfo(tz)
    t = datetime.fromtimestamp(now_ms / 1000, tz=z)
    nxt = datetime(t.year, t.month, t.day, tzinfo=z) + timedelta(days=1)
    # 重新本地化：跨夏令时那天 +1 天不等于 +24 小时
    nxt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=z)
    return int(nxt.astimezone(timezone.utc).timestamp() * 1000)


def overtime_threshold_ms(rec: dict, cfg: dict) -> int:
    est = rec.get("estimate")
    if est:
        return int(est * cfg["overtime_ratio"] * MIN_MS)
    return cfg["overtime_cap_minutes"] * MIN_MS


def evaluate(now_ms: int, cfg: dict, active: list[dict],
             last_activity_ms: Optional[int]) -> list[dict]:
    """返回此刻该推的提醒（尚未去重）。

    `active`：计时中/已暂停的记录（带 `actual_ms`，计时中的已按 now 算好）。
    每条提醒带一个 `key`，调用方据此去重（同一个 key 只推一次）。
    """
    if now_ms < (cfg.get("muted_until") or 0) or is_quiet(now_ms, cfg):
        return []
    out = []
    running = [r for r in active if r["status"] == "running"]
    for r in running:
        limit = overtime_threshold_ms(r, cfg)
        if r["actual_ms"] >= limit:
            out.append({"kind": "overtime", "key": f"overtime:{r['id']}", "record": r,
                        "minutes": r["actual_ms"] // MIN_MS})
    if not running and last_activity_ms is not None:
        start = last_activity_ms
        qe = last_quiet_exit(now_ms, start, cfg)
        if qe is not None:
            start = max(start, qe)
        idle = now_ms - start
        if idle >= cfg["idle_remind_minutes"] * MIN_MS:
            out.append({"kind": "idle", "key": f"idle:{last_activity_ms}",
                        "minutes": idle // MIN_MS})
    return out


def fmt_minutes(m: int) -> str:
    if m < 60:
        return f"{m} 分钟"
    h, r = divmod(m, 60)
    return f"{h} 小时 {r} 分" if r else f"{h} 小时"


def message(item: dict) -> tuple[str, str]:
    """(标题, 正文)。中性文案：问「是不是忘了」，不说「超时」「浪费」。"""
    if item["kind"] == "idle":
        return ("现在在做什么？", f"已经 {fmt_minutes(item['minutes'])} 没有计时了。想记就点开，不想记就划掉。")
    r = item["record"]
    name = r.get("name") or "未命名"
    if r.get("estimate"):
        body = f"「{name}」已计时 {fmt_minutes(item['minutes'])}（预估 {fmt_minutes(r['estimate'])}）。是不是忘了暂停或结束？"
    else:
        body = f"「{name}」已计时 {fmt_minutes(item['minutes'])}。是不是忘了暂停或结束？"
    return ("还在计时", body)
