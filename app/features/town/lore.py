"""小镇居民的「设定」：从酒馆角色卡 / 用户人设里读出来，**不压缩**地交给模型。

**只读**酒馆文件（与 tavern_catalog 同一纪律）。居民入住时把设定**快照**进库：
① 之后酒馆里改卡不会让一个跑了 30 天的小镇里的人突然换了性格；
② 同一个人每次调用的设定前缀逐字相同，DeepSeek 的前缀缓存才能命中
   （缓存命中价是未命中的 1/50，而设定正是每次调用里最大的那块）。
要跟上酒馆里的修改，走显式的「从酒馆重新读取」。

世界书取哪一本：卡的 `extensions.world` 指向的那本独立世界书**优先**于卡内嵌的
`character_book` —— 酒馆运行时用的是前者（导入时内嵌书会被拷成独立文件，之后
机主改的都是那份）。实测 339 张有链接的卡里 144 张的独立文件还在，其余回落到
内嵌书。`charLore.extraBooks`（机主后挂的额外书，多是 Memory_Context 聊天记忆）
**刻意不读**：小镇的初始状态只有人物本身的设定，不带机主和这个角色的聊天史。

条目分三类：
- 常驻（蓝灯 constant）：每次都给，原样不压缩；
- 关键词（绿灯）：按本次调用的上下文匹配关键词才给，规则仿酒馆（含二级关键词逻辑、
  有限递归），**不做概率**——同一份输入应该得到同一份设定；
- 排除：明显是给酒馆前端用的机关（MVU 变量、EJS 脚本、状态栏、输出格式要求、
  HTML）。实测 389 张卡里 249 张带这类条目，原样喂进去模型会开始输出状态栏、
  或把「每次回复必须 800 字」当成小镇的规则。判据是启发式的，**一定会误判**，
  所以排除理由逐条留着给面板展示，并允许按条目手动开/关（`overrides`）。
"""

from __future__ import annotations

import base64
import json
import re
import struct
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.settings import settings

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_CHARA_KEYS = (b"ccv3", b"chara")

# 酒馆 selectiveLogic：二级关键词的组合方式
LOGIC_AND_ANY, LOGIC_NOT_ALL, LOGIC_NOT_ANY, LOGIC_AND_ALL = 0, 1, 2, 3

# 递归激活最多几轮。酒馆默认开递归、上限不限；这里给 2 轮：够把「人名 → 此人
# 的详细设定」这种两级链路拉出来，又不至于一个常见词把整本书连锁点亮。
MAX_RECURSION = 2


# ── 读文件 ──────────────────────────────────────────────


def user_dir() -> Path:
    return Path(settings.tavern_data_dir).expanduser() / settings.tavern_user


def read_png_card(path: Path) -> Optional[dict]:
    """解出角色卡 PNG 里的 JSON（ccv3 优先于 chara，与 tavern_catalog 一致）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] != _PNG_MAGIC:
        return None
    found: dict[bytes, dict] = {}
    i = 8
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i : i + 4])[0]
        ctype = data[i + 4 : i + 8]
        if ctype == b"IEND":
            break
        if ctype == b"tEXt":
            key, _, value = data[i + 8 : i + 8 + length].partition(b"\x00")
            key = key.lower()
            if key in _CHARA_KEYS and key not in found:
                try:
                    obj = json.loads(base64.b64decode(value))
                    if isinstance(obj, dict):
                        found[key] = obj
                except Exception:  # noqa: BLE001 —— 单个坏块不该让整张卡失效
                    pass
        i += 12 + length
    for key in _CHARA_KEYS:
        if key in found:
            return found[key]
    return None


def _safe_child(base: Path, name: str) -> Path:
    """只允许 base 目录下的一级文件名 —— 文件名来自前端，不能带路径。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"非法文件名: {name!r}")
    return base / name


def card_path(file: str) -> Path:
    return _safe_child(user_dir() / "characters", file)


def persona_avatar_path(file: str) -> Path:
    return _safe_child(user_dir() / "User Avatars", file)


def load_world_file(name: str) -> Optional[dict]:
    if not name:
        return None
    try:
        path = _safe_child(user_dir() / "worlds", f"{name}.json")
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_personas() -> list[dict]:
    """酒馆里的用户人设（settings.json 的 power_user.personas）。"""
    try:
        s = json.loads((user_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pu = s.get("power_user") or {}
    names = pu.get("personas") or {}
    descs = pu.get("persona_descriptions") or {}
    out = []
    for file, name in names.items():
        d = descs.get(file) or {}
        text = d.get("description") or ""
        out.append({
            "file": file,
            "name": name,
            "title": d.get("title") or "",
            "chars": len(text),
            "preview": text[:120],
        })
    return out


# ── 条目归一化 ──────────────────────────────────────────


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def _norm_embedded(e: dict, idx: int) -> dict:
    ext = e.get("extensions") or {}
    return {
        "uid": str(e.get("id", idx)),
        "comment": str(e.get("comment") or e.get("name") or "").strip(),
        "content": str(e.get("content") or ""),
        "keys": _as_list(e.get("keys")),
        "secondary": _as_list(e.get("secondary_keys")) if e.get("selective") else [],
        "logic": int(ext.get("selectiveLogic") or 0),
        "constant": bool(e.get("constant")),
        "enabled": e.get("enabled", True) is not False,
        "order": int(e.get("insertion_order") or ext.get("order") or 100),
        "exclude_recursion": bool(ext.get("exclude_recursion")),
        "prevent_recursion": bool(ext.get("prevent_recursion")),
    }


def _norm_world(e: dict, idx: str) -> dict:
    return {
        "uid": str(e.get("uid", idx)),
        "comment": str(e.get("comment") or "").strip(),
        "content": str(e.get("content") or ""),
        "keys": _as_list(e.get("key")),
        "secondary": _as_list(e.get("keysecondary")) if e.get("selective", True) else [],
        "logic": int(e.get("selectiveLogic") or 0),
        "constant": bool(e.get("constant")),
        "enabled": not e.get("disable", False),
        "order": int(e.get("order") or 100),
        "exclude_recursion": bool(e.get("excludeRecursion")),
        "prevent_recursion": bool(e.get("preventRecursion")),
    }


def entries_from_world(world: dict) -> list[dict]:
    raw = world.get("entries") or {}
    items = raw.items() if isinstance(raw, dict) else enumerate(raw)
    return [_norm_world(e, str(k)) for k, e in items if isinstance(e, dict)]


def entries_from_embedded(book: dict) -> list[dict]:
    raw = book.get("entries") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [_norm_embedded(e, i) for i, e in enumerate(raw) if isinstance(e, dict)]


# ── 机关条目识别 ────────────────────────────────────────

# 注释里出现这些词基本是机关：MVU 变量系统、初始化、脚本、格式控制。
_SYS_COMMENT = re.compile(
    r"mvu|initvar|ejs|控制器|防止掉格式|掉格式|状态栏|变量|输出格式|回复格式|"
    r"格式要求|思维链|\bcot\b|破限|jailbreak|\bsys\b|system|总结|小总结|大总结",
    re.I,
)
# 正文里出现这些就是给前端/脚本执行的，不是人物设定。
_SYS_CONTENT = [
    (re.compile(r"<%"), "EJS 脚本"),
    (re.compile(r"\{\{\s*(?:get|set|add|inc|dec)(?:global)?var", re.I), "酒馆变量宏"),
    (re.compile(r"stat_data|_\.set\(|initvar|format_message_variable", re.I), "MVU 变量"),
    (re.compile(r"<\s*(?:style|script|div|html|body)\b", re.I), "HTML / 前端代码"),
    (re.compile(r"<StatusPlaceHolder|<UpdateVariable|<status", re.I), "状态栏占位"),
    (re.compile(r"(?:输出|回复|回答)格式|每次回复|字数要求|必须以.{0,10}格式", re.I), "输出格式要求"),
]


def system_reason(comment: str, content: str) -> Optional[str]:
    """疑似「给酒馆前端用的机关」返回理由，否则 None。启发式，允许误判（可手动覆盖）。"""
    if not content.strip():
        return "正文为空"
    if comment and _SYS_COMMENT.search(comment):
        return f"注释像机关（{comment[:20]}）"
    for pat, why in _SYS_CONTENT:
        if pat.search(content):
            return why
    return None


# ── 宏与清洗 ────────────────────────────────────────────

_MACRO_COMMENT = re.compile(r"\{\{//.*?\}\}", re.S)
_HTML_BLOCK = re.compile(r"<(style|script)\b.*?</\1>", re.S | re.I)
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_TAG = re.compile(r"</?[A-Za-z][^<>]{0,200}>")


def apply_macros(text: str, char: str, user: str) -> str:
    if not text:
        return ""
    text = _MACRO_COMMENT.sub("", text)
    text = re.sub(r"\{\{\s*char\s*\}\}|<BOT>", char, text, flags=re.I)
    text = re.sub(r"\{\{\s*user\s*\}\}|<USER>", user, text, flags=re.I)
    return text


def clean_prose(text: str) -> str:
    """开场白之类的正文：去掉 HTML、代码块、标签，留下文字。"""
    if not text:
        return ""
    text = _HTML_BLOCK.sub("", text)
    text = _CODE_FENCE.sub("", text)
    text = _TAG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── 激活 ────────────────────────────────────────────────


def _key_hit(key: str, text_low: str, text: str) -> bool:
    m = re.fullmatch(r"/(.+)/([a-z]*)", key, re.S)
    if m:
        flags = re.I if "i" in m.group(2) else 0
        try:
            return re.search(m.group(1), text, flags) is not None
        except re.error:
            return False
    return key.lower() in text_low


def _entry_hit(e: dict, text: str) -> bool:
    if not e["keys"]:
        return False
    low = text.lower()
    if not any(_key_hit(k, low, text) for k in e["keys"]):
        return False
    sec = e.get("secondary") or []
    if not sec:
        return True
    hits = [_key_hit(k, low, text) for k in sec]
    logic = e.get("logic", 0)
    if logic == LOGIC_AND_ALL:
        return all(hits)
    if logic == LOGIC_NOT_ALL:
        return not all(hits)
    if logic == LOGIC_NOT_ANY:
        return not any(hits)
    return any(hits)


def is_enabled(e: dict, overrides: Optional[dict] = None) -> bool:
    """条目是否参与：手动覆盖 > 酒馆里的启用开关 + 机关识别。"""
    ov = (overrides or {}).get(e["uid"])
    if ov == "on":
        return True
    if ov == "off":
        return False
    return e.get("enabled", True) and not e.get("excluded")


def split_entries(entries: Iterable[dict], overrides: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """(常驻, 关键词候选)，均已按 order 排序、已去掉不参与的。"""
    const, keyed = [], []
    for e in entries:
        if not is_enabled(e, overrides):
            continue
        (const if e["constant"] else keyed).append(e)
    const.sort(key=lambda e: e["order"])
    keyed.sort(key=lambda e: e["order"])
    return const, keyed


def activate(keyed: list[dict], scan_text: str) -> list[dict]:
    """按上下文激活关键词条目，含有限递归。返回按 order 排序的命中条目。"""
    hit: dict[str, dict] = {}
    base = scan_text or ""
    for e in keyed:
        if _entry_hit(e, base):
            hit[e["uid"]] = e
    extra = ""
    for _ in range(MAX_RECURSION):
        added = "\n".join(e["content"] for e in hit.values() if not e.get("prevent_recursion"))
        if added == extra:
            break
        extra = added
        grew = False
        for e in keyed:
            if e["uid"] in hit or e.get("exclude_recursion"):
                continue
            if _entry_hit(e, extra):
                hit[e["uid"]] = e
                grew = True
        if not grew:
            break
    return sorted(hit.values(), key=lambda e: e["order"])


def render_entries(entries: Iterable[dict]) -> str:
    parts = []
    for e in entries:
        head = f"【{e['comment']}】\n" if e.get("comment") else ""
        parts.append(head + e["content"].strip())
    return "\n\n".join(p for p in parts if p.strip())


# ── 快照 ────────────────────────────────────────────────


def _mark(entries: list[dict]) -> list[dict]:
    for e in entries:
        e["excluded"] = system_reason(e["comment"], e["content"])
    return entries


def snapshot_card(file: str, user_alias: str) -> dict:
    """读一张角色卡，产出入库用的居民快照。宏在这里展开（之后不再碰原文）。"""
    path = card_path(file)
    raw = read_png_card(path)
    if raw is None:
        raise ValueError(f"读不出角色卡：{file}")
    h = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    name = str(h.get("name") or raw.get("name") or path.stem).strip()

    def m(text: Any) -> str:
        return apply_macros(str(text or ""), name, user_alias)

    world_name = str(((h.get("extensions") or {}).get("world")) or "").strip()
    world = load_world_file(world_name)
    if world is not None:
        entries, book = entries_from_world(world), f"世界书：{world_name}"
    elif isinstance(h.get("character_book"), dict):
        entries, book = entries_from_embedded(h["character_book"]), "卡内嵌世界书"
    else:
        entries, book = [], "无世界书"
    for e in entries:
        e["content"] = m(e["content"])
        e["keys"] = [m(k) for k in e["keys"]]
        e["secondary"] = [m(k) for k in e["secondary"]]

    return {
        "kind": "card",
        "file": file,
        "name": name,
        "book": book,
        "fields": {
            "description": m(h.get("description")),
            "personality": m(h.get("personality")),
            "scenario": m(h.get("scenario")),
            "mes_example": m(h.get("mes_example")),
            "first_mes": clean_prose(m(h.get("first_mes"))),
        },
        "entries": _mark(entries),
    }


def snapshot_persona(file: str) -> dict:
    """读一份用户人设。人设里的 {{user}} 指的就是他自己。"""
    try:
        s = json.loads((user_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"读不出酒馆 settings.json：{exc}") from exc
    pu = s.get("power_user") or {}
    name = str((pu.get("personas") or {}).get(file) or "").strip()
    if not name:
        raise ValueError(f"酒馆里没有这个人设：{file}")
    d = (pu.get("persona_descriptions") or {}).get(file) or {}
    desc = apply_macros(d.get("description") or "", name, name)
    entries: list[dict] = []
    book = "无世界书"
    world = load_world_file(d.get("lorebook") or "")
    if world is not None:
        entries, book = entries_from_world(world), f"世界书：{d.get('lorebook')}"
        for e in entries:
            e["content"] = apply_macros(e["content"], name, name)
    return {
        "kind": "persona",
        "file": file,
        "name": name,
        "book": book,
        "fields": {"description": desc, "personality": "", "scenario": "",
                   "mes_example": "", "first_mes": ""},
        "entries": _mark(entries),
    }


_FIELD_TITLES = (
    ("description", "人物描述"),
    ("personality", "性格"),
    ("scenario", "原作情境"),
    ("mes_example", "说话方式示例"),
)


def render_profile(res: dict) -> str:
    """一个居民的固定设定块：卡片字段 + 常驻条目。**每次调用逐字相同**（前缀缓存）。"""
    f = res.get("fields") or {}
    parts = [f"＝＝＝ 人物设定：{res['name']} ＝＝＝"]
    for key, title in _FIELD_TITLES:
        v = (f.get(key) or "").strip()
        if v:
            parts.append(f"【{title}】\n{v}")
    const, _ = split_entries(res.get("entries") or [], res.get("overrides"))
    body = render_entries(const)
    if body:
        parts.append(body)
    note = (res.get("note") or "").strip()
    if note:
        parts.append("【在小镇里的补充设定】（优先于上文）\n" + note)
    first = (f.get("first_mes") or "").strip()
    if first:
        parts.append(
            "【穿越前的最后一幕】（原作的开场片段。这是来小镇之前发生的事，"
            "用来把握此人的处境、口吻和文风）\n" + first
        )
    return "\n\n".join(parts)


def lore_stats(res: dict) -> dict:
    const, keyed = split_entries(res.get("entries") or [], res.get("overrides"))
    entries = res.get("entries") or []
    return {
        "profile_chars": len(render_profile(res)),
        "constant": len(const),
        "keyed": len(keyed),
        "keyed_chars": sum(len(e["content"]) for e in keyed),
        "excluded": sum(1 for e in entries if not is_enabled(e, res.get("overrides"))),
        "total": len(entries),
    }
