"""AI 小镇的一天（一步）：早上各自选地点 → 白天按地点演场景 → 晚上各自写日记。

调用次数 = 居民数（计划）+ 有人的地点数（场景）+ 居民数（日记）。三段之间有依赖、
段内互相独立，所以段内并行、段间串行。

**断点续跑**：每完成一个调用立刻落库（计划写进 `town_days.plans`、场景写进
`scenes`、日记写进 `town_memories`）。中途失败或服务重启后再点「下一天」，从
第一个缺的调用接着做，已完成的不重跑——一天十几次调用，某一次超时就整天重来
等于白扔钱，而且重跑出来的是另一套剧情，和已经存下的日记对不上。

运行状态只在进程内（`_tasks`）。库里 `status=running` 但进程里没有对应任务，就是
被重启打断了，读的时候按 `interrupted` 报，不需要启动钩子去修库。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from typing import Any, Optional

import httpx

from app.core.llm_json import extract_json_object
from app.core.logging import get_logger
from app.core.settings import settings
from app.features.town.repo import get_town_repo, now
from app.features.town import lore as L
from app.features.town import prompts as P

logger = get_logger("town")

DEFAULT_FACILITIES = (
    ("school", "学校", ""),
    ("hospital", "医院", ""),
    ("dungeon", "地下城", ""),
    ("shop", "商店", ""),
)
HOME_NAMES = "一二三四五六七八九十"

# 用户人设通常只写「内核」、刻意不写外在身份（张超那份原文就是「每次扮演的物种、
# 性别、年龄……都可以完全不同」）。小镇里他得有一具具体的身体，否则每一场都会
# 被重新发明一遍。默认交给第一天的场景来定、之后靠日记保持一致；机主可在面板改写。
PERSONA_DEFAULT_NOTE = (
    "这份设定只规定了内核、没有规定外在身份。在小镇里他就是他本人：一个普通的成年人，"
    "穿越前生活在现代都市。外貌、年龄、职业等细节第一天自然带出，之后保持一致。"
)

# 进程内：town_id -> asyncio.Task / 进度
_tasks: dict[str, asyncio.Task] = {}
_progress: dict[str, dict] = {}


class TownError(Exception):
    pass


# ── 建镇 ────────────────────────────────────────────────


def home_name(i: int) -> str:
    return f"{HOME_NAMES[i]}号屋" if i < len(HOME_NAMES) else f"{i + 1}号屋"


def create_town(name: str, members: list[dict], *, me: Optional[str] = None) -> str:
    """members: [{"kind": "card"|"persona", "file": ...}]，顺序即居民序号。

    `me`：哪份人设是「我」。它的名字会写进别的卡里的 {{user}} 吗？**不会** ——
    原作里的 {{user}} 一律换成「原作主角」（见 town_prompts.ORIGINAL_USER）。
    """
    if not members:
        raise TownError("至少要有一位居民")
    if len(members) > len(HOME_NAMES):
        raise TownError(f"最多 {len(HOME_NAMES)} 位居民")
    snaps = []
    for m in members:
        try:
            if m.get("kind") == "persona":
                snaps.append(L.snapshot_persona(m["file"]))
            else:
                snaps.append(L.snapshot_card(m["file"], P.ORIGINAL_USER))
        except (ValueError, KeyError) as exc:
            raise TownError(str(exc)) from exc
    seen = set()
    for s in snaps:
        if s["name"] in seen:
            raise TownError(f"有两位居民同名：{s['name']}")
        seen.add(s["name"])

    places = [{"id": f"home-{i + 1}", "name": home_name(i), "kind": "home", "desc": ""}
              for i in range(len(snaps))]
    places += [{"id": pid, "name": n, "kind": "public", "desc": d} for pid, n, d in DEFAULT_FACILITIES]

    repo = get_town_repo()
    town_id = repo.create_town({"name": name.strip() or "无名小镇", "places": places,
                                "lore": P.DEFAULT_TOWN_LORE})
    for i, s in enumerate(snaps):
        note = PERSONA_DEFAULT_NOTE if s["kind"] == "persona" else ""
        repo.add_resident({**s, "town_id": town_id, "order": i, "home": f"home-{i + 1}",
                           "is_me": s["kind"] == "persona", "overrides": {}, "note": note})
    logger.info("建镇 %s：%s", town_id, "、".join(s["name"] for s in snaps))
    return town_id


def resnapshot(rid: str) -> dict:
    """从酒馆重新读取一位居民的设定（保留手动开关）。"""
    repo = get_town_repo()
    r = repo.get_resident(rid)
    if not r:
        raise TownError("居民不存在")
    try:
        s = L.snapshot_persona(r["file"]) if r["kind"] == "persona" else L.snapshot_card(r["file"], P.ORIGINAL_USER)
    except ValueError as exc:
        raise TownError(str(exc)) from exc
    s.pop("name", None)  # 名字不跟着变：日记、印象里都是旧名字
    repo.update_resident(rid, {**s, "snapshot_at": now()})
    return repo.get_resident(rid)


# ── 派生状态 ────────────────────────────────────────────


def _rid(r: dict) -> str:
    return r["_id"]


def place_facts(town_id: str, before_day: int) -> dict[str, list[tuple[int, str]]]:
    """各地点已确立的事实（来自之前各天场景的 place_facts）。"""
    out: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for d in get_town_repo().days_of(town_id, upto=before_day - 1):
        for pid, sc in (d.get("scenes") or {}).items():
            for f in sc.get("place_facts") or []:
                out[pid].append((d["day"], f))
    return out


def visited(town_id: str, rid: str, before_day: int) -> set[str]:
    return {m.get("place") for m in get_town_repo().memories_of(town_id, rid, before=before_day)}


def diaries_for(town_id: str, rid: str, before_day: int) -> list[tuple[int, str]]:
    mems = get_town_repo().memories_of(town_id, rid, before=before_day)
    cap = settings.town_memory_days_cap
    items = [(m["day"], m.get("diary") or "") for m in mems]
    if len(items) > cap:
        items = [(items[0][0], f"（更早的 {len(items) - cap} 篇日记已略，只保留最近 {cap} 篇）")] + items[-cap:]
    return items


def match_place(raw: str, places: list[dict], home_id: str) -> tuple[str, Optional[str]]:
    """把模型给的地点名对到地点 id。返回 (id, 警告)。"""
    s = re.sub(r"[（(].*?[)）]", "", str(raw or "")).strip()
    if not s:
        return home_id, "没给地点，留在家里"
    for p in places:
        if s == p["name"]:
            return p["id"], None
    if s in ("家", "我家", "自己家", "家里", "住处"):
        return home_id, None
    for p in places:
        if p["name"] in s or s in p["name"]:
            return p["id"], None
    return home_id, f"地点「{s[:20]}」不在列表里，按留在家里处理"


# ── 调用 DeepSeek ────────────────────────────────────────


def _extract_json(text: str) -> Optional[dict]:
    return extract_json_object(text)


def cost_of(usage: dict) -> dict:
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or max(0, prompt - hit))
    out = int(usage.get("completion_tokens") or 0)
    yuan = (hit * settings.town_price_hit + miss * settings.town_price_miss
            + out * settings.town_price_out) / 1_000_000
    return {"yuan": round(yuan, 6), "input": prompt, "cache_hit": hit, "output": out, "calls": 1}


async def call_json(client: httpx.AsyncClient, messages: list[dict], max_tokens: int,
                    *, required: str, town_id: str, day: int) -> tuple[dict, list[str]]:
    """调一次，解析 JSON，必须含 `required` 字段。失败重试一次；再失败抛 TownError。

    每次调用（含失败的）都记账 —— 失败的调用一样花了钱。
    """
    if not settings.deepseek_api_key:
        raise TownError("DeepSeek API key 未配置（.env 的 deepseek_api_key）")
    body = {
        "model": settings.town_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "thinking": {"type": "enabled" if settings.town_thinking else "disabled"},
    }
    last = ""
    for attempt in range(2):
        try:
            resp = await client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json=body,
            )
        except httpx.TimeoutException:
            last = f"请求超时（{settings.town_timeout}s）"
            continue
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code in (400, 401, 402, 422):
                break  # 参数/鉴权/余额问题，重试也一样
            await asyncio.sleep(3)
            continue
        data = resp.json()
        get_town_repo().add_cost(town_id, day, cost_of(data.get("usage") or {}))
        choice = (data.get("choices") or [{}])[0]
        obj = _extract_json((choice.get("message") or {}).get("content") or "")
        warns = []
        if choice.get("finish_reason") not in (None, "stop"):
            warns.append(f"生成提前结束（finish_reason={choice.get('finish_reason')}），内容可能被截断")
        if obj and str(obj.get(required) or "").strip():
            return obj, warns
        last = f"回复里没有可用的 {required}" + (f"；{warns[0]}" if warns else "")
    raise TownError(last or "调用失败")


# ── 一天 ────────────────────────────────────────────────


def _set_progress(town_id: str, **kw: Any) -> None:
    _progress.setdefault(town_id, {}).update(kw)


async def _gather_limited(coros: list, limit: int) -> list:
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)


async def run_day(town_id: str) -> dict:
    repo = get_town_repo()
    town = repo.get_town(town_id)
    if not town:
        raise TownError("小镇不存在")
    residents = repo.residents_of(town_id)
    places = town["places"]
    by_pid = {p["id"]: p for p in places}
    by_rid = {_rid(r): r for r in residents}
    day = int(town.get("day") or 0) + 1
    doc = repo.ensure_day(town_id, day)
    repo.set_day(town_id, day, {"status": "running", "error": None, "started_at": now()})
    t0 = time.monotonic()
    timeout = httpx.Timeout(settings.town_timeout, connect=20)
    facts_all = place_facts(town_id, day)
    lore = town.get("lore") or ""
    diaries = {_rid(r): diaries_for(town_id, _rid(r), day) for r in residents}

    async with httpx.AsyncClient(timeout=timeout) as client:
        # ① 早上：选地点
        plans: dict = dict(doc.get("plans") or {})
        todo = [r for r in residents if _rid(r) not in plans]
        _set_progress(town_id, day=day, phase="早上：各自决定去哪", done=len(residents) - len(todo), total=len(residents))

        async def plan_one(r: dict):
            seen = visited(town_id, _rid(r), day)
            facts = {pid: [f for _, f in fs] for pid, fs in facts_all.items() if pid in seen}
            msgs = P.plan_messages(r, day=day, places=places, facts=facts, diaries=diaries[_rid(r)],
                                   home_name=by_pid[r["home"]]["name"], town_lore=lore)
            obj, warns = await call_json(client, msgs, settings.town_plan_max_tokens,
                                         required="place", town_id=town_id, day=day)
            pid, w = match_place(obj.get("place"), places, r["home"])
            if w:
                warns.append(w)
            plan = {"place": pid, "raw_place": str(obj.get("place") or "")[:40],
                    "thought": str(obj.get("thought") or "")[:400],
                    "intent": str(obj.get("intent") or "")[:400], "warnings": warns}
            repo.set_day(town_id, day, {f"plans.{_rid(r)}": plan})
            plans[_rid(r)] = plan
            _progress[town_id]["done"] += 1

        await _raise_first(await _gather_limited([plan_one(r) for r in todo], settings.town_concurrency))

        # ② 白天：按地点演场景
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in residents:
            groups[plans[_rid(r)]["place"]].append(r)
        scenes: dict = dict((repo.get_day(town_id, day) or {}).get("scenes") or {})
        todo_p = [pid for pid in groups if not (scenes.get(pid) or {}).get("body")]
        _set_progress(town_id, phase="白天：各地点发生的事", done=len(groups) - len(todo_p), total=len(groups))

        async def scene_one(pid: str):
            members = groups[pid]
            place = by_pid[pid]
            owner = next((r for r in residents if r["home"] == pid), None)
            msgs = P.scene_messages(
                members, day=day, place=place, facts=[f for _, f in facts_all.get(pid, [])],
                plans={_rid(m): plans[_rid(m)] for m in members},
                memories={_rid(m): diaries[_rid(m)] for m in members},
                home_owner=owner,
                target_chars=(settings.town_scene_chars_min, settings.town_scene_chars_max),
                town_lore=lore,
            )
            obj, warns = await call_json(client, msgs, settings.town_scene_max_tokens,
                                         required="body", town_id=town_id, day=day)
            body = str(obj.get("body") or "").strip()
            if len(body) < settings.town_scene_chars_min * 0.6:
                warns.append(f"正文偏短：{len(body)} 字")
            facts = [str(f).strip()[:120] for f in (obj.get("place_facts") or []) if str(f).strip()][:3]
            sc = {"place": pid, "place_name": place["name"], "members": [_rid(m) for m in members],
                  "title": str(obj.get("title") or "").strip()[:30], "body": body,
                  "summary": str(obj.get("summary") or "").strip()[:80], "place_facts": facts,
                  "warnings": warns}
            repo.set_day(town_id, day, {f"scenes.{pid}": sc})
            scenes[pid] = sc
            _progress[town_id]["done"] += 1

        await _raise_first(await _gather_limited([scene_one(p) for p in todo_p], settings.town_concurrency))

        # ③ 晚上：写日记
        todo_r = [r for r in residents if not repo.has_memory(town_id, _rid(r), day)]
        _set_progress(town_id, phase="晚上：各自写日记", done=len(residents) - len(todo_r), total=len(residents))

        async def diary_one(r: dict):
            pid = plans[_rid(r)]["place"]
            sc = scenes[pid]
            others = [by_rid[x]["name"] for x in sc["members"] if x != _rid(r)]
            msgs = P.diary_messages(r, day=day, place=by_pid[pid], scene=sc, others=others,
                                    diaries=diaries[_rid(r)],
                                    target_chars=(settings.town_diary_chars_min, settings.town_diary_chars_max),
                                    town_lore=lore)
            obj, warns = await call_json(client, msgs, settings.town_diary_max_tokens,
                                         required="diary", town_id=town_id, day=day)
            imp = obj.get("impressions") if isinstance(obj.get("impressions"), dict) else {}
            repo.put_memory(town_id, _rid(r), day, {
                "place": pid,
                "diary": str(obj.get("diary") or "").strip(),
                "mood": str(obj.get("mood") or "").strip()[:12],
                "goal": str(obj.get("goal") or "").strip()[:200],
                # 键是人名，当 Mongo 字段名用：「.」与开头的「$」不合法（人名里真有「.」）
                "impressions": {str(k).replace(".", "·").lstrip("$")[:30] or "?": str(v)[:200]
                                for k, v in list(imp.items())[:12]},
                "warnings": warns,
            })
            _progress[town_id]["done"] += 1

        await _raise_first(await _gather_limited([diary_one(r) for r in todo_r], settings.town_concurrency))

    repo.set_day(town_id, day, {"status": "done", "finished_at": now(), "error": None,
                                "seconds": round(time.monotonic() - t0, 1)})
    repo.set_town_day(town_id, day)
    logger.info("小镇 %s 第 %d 天完成（%d 个场景）", town_id, day, len(groups))
    return {"day": day}


async def _raise_first(results: list) -> None:
    errs = [r for r in results if isinstance(r, BaseException)]
    if errs:
        msg = "；".join(dict.fromkeys(str(e) for e in errs))
        raise TownError(f"{len(errs)} 个调用失败：{msg}"[:600])


async def _runner(town_id: str) -> None:
    repo = get_town_repo()
    town = repo.get_town(town_id) or {}
    day = int(town.get("day") or 0) + 1
    try:
        await run_day(town_id)
    except Exception as exc:  # noqa: BLE001 —— 后台任务，异常必须落库而不是丢进事件循环
        logger.exception("小镇 %s 第 %d 天失败", town_id, day)
        repo.set_day(town_id, day, {"status": "failed", "error": str(exc)[:600]})
    finally:
        _tasks.pop(town_id, None)
        _progress.pop(town_id, None)


def start_day(town_id: str) -> dict:
    if town_id in _tasks:
        raise TownError("这个小镇的一天正在进行中")
    if not get_town_repo().get_town(town_id):
        raise TownError("小镇不存在")
    _tasks[town_id] = asyncio.create_task(_runner(town_id))
    return {"started": True}


def is_running(town_id: str) -> bool:
    return town_id in _tasks


def progress(town_id: str) -> Optional[dict]:
    p = _progress.get(town_id)
    return dict(p) if p else None


def effective_status(day_doc: dict) -> str:
    st = day_doc.get("status") or ""
    if st == "running" and day_doc.get("town_id") not in _tasks:
        return "interrupted"
    return st


# ── 给前端的视图 ────────────────────────────────────────


def resident_view(r: dict, *, full: bool = False) -> dict:
    repo = get_town_repo()
    latest = repo.latest_memory(r["town_id"], _rid(r))
    out = {
        "id": _rid(r), "name": r["name"], "kind": r["kind"], "file": r["file"],
        "home": r["home"], "order": r["order"], "is_me": r.get("is_me", False),
        "book": r.get("book"), "stats": L.lore_stats(r), "note": r.get("note") or "",
        "mood": (latest or {}).get("mood"), "goal": (latest or {}).get("goal"),
        "impressions": (latest or {}).get("impressions") or {},
        "last_day": (latest or {}).get("day"),
        "snapshot_at": r.get("snapshot_at"),
    }
    if full:
        ov = r.get("overrides") or {}
        out["entries"] = [
            {"uid": e["uid"], "comment": e["comment"], "constant": e["constant"],
             "keys": e["keys"][:8], "chars": len(e["content"]), "enabled_in_tavern": e.get("enabled", True),
             "excluded": e.get("excluded"), "override": ov.get(e["uid"]),
             "active": L.is_enabled(e, ov), "preview": e["content"][:160]}
            for e in sorted(r.get("entries") or [], key=lambda e: (not e["constant"], e["order"]))
        ]
        out["fields"] = {k: len(v or "") for k, v in (r.get("fields") or {}).items()}
        out["memories"] = [
            {k: m.get(k) for k in ("day", "place", "diary", "mood", "goal", "impressions", "warnings")}
            for m in repo.memories_of(r["town_id"], _rid(r))
        ]
    return out


def town_view(town_id: str) -> dict:
    repo = get_town_repo()
    town = repo.get_town(town_id)
    if not town:
        raise TownError("小镇不存在")
    residents = repo.residents_of(town_id)
    days = repo.day_summaries(town_id)
    for d in days:
        d["status"] = effective_status(d)
        d.pop("_id", None)
    # 地图上显示「最近一天」每个人在哪
    latest = next((d for d in reversed(days) if d["status"] == "done"), None)
    where: dict[str, str] = {}
    if latest:
        for pid, sc in (latest.get("scenes") or {}).items():
            for rid in sc.get("members") or []:
                where[rid] = pid
    facts = place_facts(town_id, int(town.get("day") or 0) + 1)
    return {
        "id": town_id, "name": town["name"], "day": town.get("day", 0), "cost": town.get("cost"),
        "lore": town.get("lore") or "",
        "places": [{**p, "facts": [f for _, f in facts.get(p["id"], [])]} for p in town["places"]],
        "residents": [resident_view(r) for r in residents],
        "where": where, "days": days,
        "running": is_running(town_id), "progress": progress(town_id),
        "model": settings.town_model, "thinking": settings.town_thinking,
    }


def day_view(town_id: str, day: int) -> dict:
    d = get_town_repo().get_day(town_id, day)
    if not d:
        raise TownError(f"第 {day} 天不存在")
    d.pop("_id", None)
    d["status"] = effective_status(d)
    return d


def rollback(town_id: str, to_day: int) -> dict:
    if is_running(town_id):
        raise TownError("正在进行中，不能回退")
    town = get_town_repo().get_town(town_id)
    if not town:
        raise TownError("小镇不存在")
    if to_day < 0:
        raise TownError("天数不能为负")
    return get_town_repo().rollback(town_id, to_day)


def update_places(town_id: str, places: list[dict]) -> None:
    """改公共设施（加/删/改名/描述）。居民房不许删 —— 它们和居民绑着。"""
    repo = get_town_repo()
    town = repo.get_town(town_id)
    if not town:
        raise TownError("小镇不存在")
    homes = [p for p in town["places"] if p["kind"] == "home"]
    old_public = {p["id"]: p for p in town["places"] if p["kind"] != "home"}
    home_desc = {p.get("id"): p.get("desc", "") for p in places if p.get("kind") == "home"}
    out = [{**h, "desc": str(home_desc.get(h["id"], h.get("desc", "")))[:300]} for h in homes]
    names = {h["name"] for h in homes}
    for p in places:
        if p.get("kind") == "home":
            continue
        name = str(p.get("name") or "").strip()[:20]
        if not name or name in names:
            continue
        names.add(name)
        pid = p.get("id") if p.get("id") in old_public else f"p-{int(time.time() * 1000) % 10**9}-{len(out)}"
        out.append({"id": pid, "name": name, "kind": "public", "desc": str(p.get("desc") or "")[:300]})
    repo.update_town(town_id, {"places": out})


def set_lore(town_id: str, lore: str) -> None:
    repo = get_town_repo()
    if not repo.get_town(town_id):
        raise TownError("小镇不存在")
    repo.update_town(town_id, {"lore": str(lore or "").strip()[:5000]})


def set_override(rid: str, uid: str, value: Optional[str]) -> None:
    if value not in ("on", "off", None):
        raise TownError("取值只能是 on / off / null")
    repo = get_town_repo()
    r = repo.get_resident(rid)
    if not r:
        raise TownError("居民不存在")
    ov = dict(r.get("overrides") or {})
    if value is None:
        ov.pop(uid, None)
    else:
        ov[uid] = value
    repo.update_resident(rid, {"overrides": ov})


def set_note(rid: str, note: str) -> None:
    repo = get_town_repo()
    if not repo.get_resident(rid):
        raise TownError("居民不存在")
    repo.update_resident(rid, {"note": str(note or "").strip()[:3000]})


def entry_content(rid: str, uid: str) -> str:
    r = get_town_repo().get_resident(rid)
    for e in (r or {}).get("entries") or []:
        if e["uid"] == uid:
            return e["content"]
    raise TownError("条目不存在")


def profile_text(rid: str) -> str:
    r = get_town_repo().get_resident(rid)
    if not r:
        raise TownError("居民不存在")
    return L.render_profile(r)


def jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o)))
