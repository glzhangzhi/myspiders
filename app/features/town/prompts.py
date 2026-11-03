"""小镇的三段提示词：早上选地点 / 白天演场景 / 晚上写日记。

**拼装顺序是为前缀缓存排的**，改之前先想清楚：

    system = 通用总则（全镇恒定）+ 人物设定块（每人恒定，按居民序号排）
    user   = 当天才有的东西（日期、地点、记忆、关键词条目、任务要求）

DeepSeek 的上下文缓存按「从头开始的公共前缀」命中。同一个人一天要被调用三次
（选地点、演场景、写日记），设定块又是每次调用里最大的一块（常驻条目中位数
1.4 万字），只要它放在最前且逐字不变，后两次就几乎全按命中价计费（未命中价
的 1/50）。把日期、记忆之类会变的东西塞进 system 开头，缓存就整段失效 ——
不报错，只是每天贵几十倍。
"""

from __future__ import annotations

from typing import Optional

from app.features.town.lore import activate, render_entries, render_profile, split_entries

# 原作卡里的 {{user}} 换成这个称呼。小镇居民是穿越来的、彼此不认识；如果直接
# 换成「我」的名字，每张卡都会以为自己和他早有一段原作里的关系。
ORIGINAL_USER = "原作主角"

COMMON_HEADER = f"""这是一个「让 AI 自己玩酒馆」的小镇模拟。几位来自不同作品世界的人物，被一股无法解释的力量带到了同一个陌生小镇，从此在这里生活。下面给出相关人物的完整设定（来自他们各自原作的角色卡与世界书）。

总则（对本次任务的每一个环节都有效）：
1. 人物严格按各自的设定行事：性格、价值观、说话方式、欲望、底线、能力都以设定为准。不同的人就该不同 —— 别把所有人写成同一种温和、懂事、乐于社交的人。设定里冷漠的人就冷漠，危险的人就危险。
2. 设定里的「{ORIGINAL_USER}」指原作中与该人物对戏的另一方。那个人不在小镇；原作里的其他人物也都不在小镇。人物记得他们，但见不到他们。
3. 设定里写给原作扮演用的规则（对{ORIGINAL_USER}的特殊安排、开场流程、输出格式、状态栏、数值系统）在小镇里都不适用 —— 只取「这个人是谁」。
4. 人物保留自己的记忆、性格、身体和技能。原作里的超自然能力在小镇还能不能用、能用到什么程度，第一次尝试时才见分晓；一旦写定，之后必须前后一致。
5. 每个人只知道自己亲眼所见、亲耳所闻的事。别人的设定、过去、内心想法、名字，在对方说出口或者有途径得知之前，都是不知道的。"""


# 小镇本身的设定（每个镇一份，面板可改）。并行生成的几场戏互相看不见，
# 「镇上有没有别人」这种全局事实不写死，就会一场说空无一人、另一场说街上人来人往。
DEFAULT_TOWN_LORE = (
    "小镇里除了这几位居民，没有任何其他人：没有店员、没有路人、没有原住民。"
    "店铺和设施都开着门、东西齐全，但没人经营。小镇外围被浓雾包围，走进雾里会发现自己又回到了镇上。"
)


def system_for(residents: list[dict], town_lore: str = "") -> str:
    """system 消息：总则 + 小镇设定 + 若干人的设定块。residents 必须已按居民序号排好。

    小镇设定放在人物设定**之前**：它对同一个镇的所有调用都一样，放前面能让
    所有人共享这一段缓存；放后面会被前面各不相同的人物设定隔断。
    """
    head = COMMON_HEADER
    if town_lore.strip():
        head += "\n\n【小镇设定】（所有人物所在的这个小镇本身的客观事实）\n" + town_lore.strip()
    return head + "\n\n" + "\n\n".join(render_profile(r) for r in residents)


def keyed_lore(res: dict, scan_text: str) -> str:
    """本次调用按上下文激活的关键词条目（绿灯）。"""
    _, keyed = split_entries(res.get("entries") or [], res.get("overrides"))
    hits = activate(keyed, scan_text)
    return render_entries(hits)


# ── 地点描述 ────────────────────────────────────────────


def describe_places(places: list[dict], facts: dict[str, list[str]], *, viewer_home: Optional[str] = None) -> str:
    lines = []
    for p in places:
        label = p["name"]
        if viewer_home and p["id"] == viewer_home:
            label += "（你的家）"
        elif p["kind"] == "home":
            label += "（居民房，住着别人）"
        known = facts.get(p["id"]) or []
        desc = p.get("desc") or ""
        tail = []
        if desc:
            tail.append(desc)
        if known:
            tail.append("已知：" + "；".join(known[-8:]))
        lines.append(f"- {label}" + (f" —— {' '.join(tail)}" if tail else ""))
    return "\n".join(lines)


def render_memories(diaries: list[tuple[int, str]]) -> str:
    if not diaries:
        return "（还没有。这是你来到小镇的第一天。）"
    return "\n\n".join(f"〔第{d}天〕\n{text}" for d, text in diaries)


# ── 早上：选地点 ────────────────────────────────────────


def plan_messages(res: dict, *, day: int, places: list[dict], facts: dict, diaries: list[tuple[int, str]],
                  home_name: str, scan_extra: str = "", town_lore: str = "") -> list[dict]:
    if day == 1:
        situation = (
            f"今天是第 1 天。早上，你在一间陌生的屋子里醒来 —— 门牌上写着「{home_name}」。"
            "窗外是一个你从没见过的小镇。你不知道自己是怎么来的，只隐约觉得这里以后就是你住的地方了。"
            "你醒来时屋里只有你一个人。"
        )
    else:
        situation = f"今天是你来到小镇的第 {day} 天。早上，你在自己家（{home_name}）醒来。"

    lore = keyed_lore(res, "\n".join([scan_extra, *(t for _, t in diaries[-3:])]))
    user = [
        situation,
        "【小镇的地点】\n" + describe_places(places, facts, viewer_home=res["home"]),
        "【你在小镇的日记】\n" + render_memories(diaries),
    ]
    if lore:
        user.append("【与此相关的设定】\n" + lore)
    user.append(
        f"""【现在决定】你是{res['name']}。今天一整天你打算待在哪个地点？完全按你自己的性格、处境和日记里的打算来决定 —— 可以出门探索、可以去找某个人、也可以闭门不出。

只输出 JSON：
{{"place": "地点名，必须是上面列表里的一个", "thought": "你此刻的内心想法，一两句，用你自己的口吻", "intent": "今天打算在那里做什么，一句话"}}"""
    )
    return [
        {"role": "system", "content": system_for([res], town_lore)},
        {"role": "user", "content": "\n\n".join(user)},
    ]


# ── 白天：演场景 ────────────────────────────────────────


def scene_messages(members: list[dict], *, day: int, place: dict, facts: list[str],
                   plans: dict[str, dict], memories: dict[str, list[tuple[int, str]]],
                   home_owner: Optional[dict], target_chars: tuple[int, int],
                   town_lore: str = "") -> list[dict]:
    names = "、".join(m["name"] for m in members)
    solo = len(members) == 1
    head = [f"【第 {day} 天 · {place['name']}】"]
    if place["kind"] == "home":
        owner = home_owner["name"] if home_owner else "某位居民"
        head.append(f"这里是{owner}的家。")
    if place.get("desc"):
        head.append(place["desc"])
    if facts:
        head.append("关于这个地点，之前的场景里已经确定的事实（若与【小镇设定】冲突，以小镇设定为准）：\n"
                    + "\n".join(f"- {f}" for f in facts))
    elif place["kind"] != "home":
        head.append("这个地点还没有人来过 —— 它具体是什么样子（建筑、布置、里面有什么）由这一场来揭晓，但不能与【小镇设定】矛盾。")
    if day == 1:
        head.append("今天是所有人来到小镇的第一天：每个人都是早上在一间陌生屋子里醒来，不知道自己为何在此。")

    blocks = ["\n".join(head), f"【今天在这里的人】{names}"]
    scan = "\n".join([place["name"], *facts])
    for m in members:
        p = plans.get(m["_id"]) or {}
        di = memories.get(m["_id"]) or []
        part = [f"＝ {m['name']} ＝",
                f"今天为什么来这里（内心想法，别人不知道）：{p.get('thought') or '—'}",
                f"打算：{p.get('intent') or '—'}",
                "此人在小镇的日记：\n" + render_memories(di)]
        blocks.append("\n".join(part))
        scan += "\n" + "\n".join([p.get("intent") or "", *(t for _, t in di[-3:])])
    scan += "\n" + names
    lore_parts = []
    for m in members:
        lore = keyed_lore(m, scan)
        if lore:
            lore_parts.append(f"＝ {m['name']} 相关的设定 ＝\n{lore}")
    if lore_parts:
        blocks.append("【与今天相关的设定条目】\n" + "\n\n".join(lore_parts))

    lo, hi = target_chars
    if solo:
        task = f"""【任务】写出{names}今天独自在这里度过的一天。一个人的时候也有戏：探索、发现、回忆、自言自语、做出某个决定，或者遇到一件只发生在这个地点的事。"""
    else:
        task = f"""【任务】写出今天在这个地点，{names}之间发生的事。这是整个模拟最重要的部分：人物怎么注意到彼此、怎么开口、说了什么、怎么试探、合得来还是合不来。"""
    blocks.append(
        task + f"""

写作要求：
- 小说体，第三人称，有对话、有动作、有神态。篇幅 {lo}–{hi} 字。
- 每个人都必须像他自己：说话方式、用词、态度贴合设定，别让所有人用同一种腔调。
- 遵守信息边界：人物只能说出、利用自己知道的事。初次见面的人不知道对方名字，除非对方说了。内心想法只能通过言行流露，别人读不到。
- 不要为了「和谐」强行让人物友好、和解、敞开心扉。冷场、拒绝、误会、冲突、各怀心思都是好戏。
- 只写这个地点、这一天、这些人。不要替不在场的人写任何言行，不要预告以后的剧情，结尾不要总结升华。
- 除了上面列出的在场者，不要凭空添加有名有姓、有台词或有行动的人物（远处的人影也算）。【小镇设定】里没有的人，就不存在。

只输出 JSON：
{{"title": "这一场的标题，十个字以内",
 "body": "正文",
 "summary": "一句话概括这一场发生了什么（给小镇日报用，三十字以内）",
 "place_facts": ["这一场新确立的、关于这个地点本身的客观事实，没有就给空数组，最多三条"]}}"""
    )
    return [
        {"role": "system", "content": system_for(members, town_lore)},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


# ── 晚上：写日记 ────────────────────────────────────────


def diary_messages(res: dict, *, day: int, place: dict, scene: dict, others: list[str],
                   diaries: list[tuple[int, str]], target_chars: tuple[int, int],
                   town_lore: str = "") -> list[dict]:
    lore = keyed_lore(res, "\n".join([scene.get("body") or "", *(t for _, t in diaries[-2:])]))
    who = "、".join(others) if others else "（只有你自己）"
    lo, hi = target_chars
    user = [
        f"【第 {day} 天的晚上】你是{res['name']}。今天你在「{place['name']}」度过，在场的其他人：{who}。",
        "【你之前的日记】\n" + render_memories(diaries),
        f"【今天在{place['name']}发生的事（旁观视角的完整记录）】\n《{scene.get('title') or ''}》\n{scene.get('body') or ''}",
    ]
    if lore:
        user.append("【与此相关的设定】\n" + lore)
    user.append(
        f"""【现在写日记】以{res['name']}的第一人称、用你自己的口吻，写今天的日记。这篇日记就是你对今天的全部记忆，明天起你只能靠它想起今天。

要求：
- 只写你亲身经历、亲眼看到、亲耳听到的；别人心里怎么想你不知道，只能猜。
- 写下今天发生了什么、你对遇到的人的看法（他们叫什么——如果你知道的话、你喜欢还是讨厌、信不信得过）、你注意到的这个小镇的事情、你接下来想做什么。
- 篇幅 {lo}–{hi} 字。

只输出 JSON：
{{"diary": "日记正文",
 "mood": "此刻的心情，两到四个字",
 "goal": "接下来最想做的一件事，一句话",
 "impressions": {{"对方的名字": "对此人的一句话看法"}}}}
impressions 只写今天在场、而且你知道名字的人；不知道名字的就不写。"""
    )
    return [
        {"role": "system", "content": system_for([res], town_lore)},
        {"role": "user", "content": "\n\n".join(user)},
    ]
