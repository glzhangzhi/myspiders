"""导出角色卡的聊天记录。

**只读**，和 tavern_catalog 同一条纪律：酒馆运行时持有这些文件，本模块只读不写。
与 tavern_sync 无关 —— 聊天记录不在 tavern_sync 的管辖范围内（它同步的是
角色卡/世界书/预设的「内容」），所以这里直接读酒馆的 chats 目录，不走 ts.mjs、
也不需要抢那把同步锁。

## 目录名认的是 PNG 文件名，不是卡内嵌的名称

这一条与 tavern_catalog 的结论**方向相反**，是本模块最容易写错的地方：

- `tavern_sync.yaml` 的「酒馆中的名称」认卡内嵌的 `name`（见 tavern_catalog）
- 聊天目录 `chats/<X>/` 里的 X 是**去掉 .png 的文件名**

酒馆源码 `src/endpoints/chats.js` 里一律是
`path.join(directories.chats, String(request.body.avatar_url).replace('.png',''))`，
avatar_url 就是文件名。实测 377 张卡里 8 张两者不一致（`default_Seraphina.png`
的卡名是 `Seraphina`），按卡名去找目录会得到「这张卡没有聊天记录」。
故入口一律收卡名，内部先经 catalog 换成文件名再拼目录。

## 消息正文取 `mes`，不取 `swipes`

一条消息可能有多个 swipe（重roll 的候选），`mes` 始终等于当前选中的那个
（`swipes[swipe_id]`）。导出要的是「这盘对话实际长什么样」，所以只取 `mes`；
把所有 swipe 都倒出来会让记录里冒出大段从未被采用的分支。

## 清理的判据是「我说了几句」，不是楼层数

`overview()` 除了楼层还数一遍 `is_user` 为真的条数。实测 112 份记录里有 7 份
楼层=1（只有开场白），其中一份 37.6KB —— 光看楼层或大小都会犹豫，但
「用户消息数 = 0」是毫不含糊的「这盘我一句话都没说过」。故总表默认按它排序。

全量解析 112 份、5620 行 JSON 实测 0.22 秒，比只数行（0.02 秒）贵一个数量级
但绝对值仍然很小，所以 overview 直接全解，不做增量。
"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.tavern.catalog import TYPE_CHARACTER, tavern_catalog

logger = get_logger("tavern_chats")

# 单个文件的导出上限。最大的一份聊天记录实测 5.3MB（约 2 万条消息），
# 留出几倍余量；真超了说明是异常文件，不该让它把内存吃穿。
_MAX_FILE_BYTES = 64 * 1024 * 1024
# 打包全部时的总量上限（未压缩）。单卡最多的一个目录实测约 10MB。
_MAX_ZIP_BYTES = 256 * 1024 * 1024

FORMATS = ("md", "txt", "jsonl")


class TavernChatError(RuntimeError):
    """预期内的用户可读错误（卡不存在、没有聊天记录、路径越界等）。"""


@dataclass(frozen=True)
class ChatFile:
    """一份聊天记录文件。"""

    file: str          # 文件名，含 .jsonl
    size: int
    modified: str      # ISO8601 UTC
    messages: int      # 消息条数（不含首行的元数据）

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "size": self.size,
            "modified": self.modified,
            "messages": self.messages,
        }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class TavernChatService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._cache_key: Optional[tuple] = None

    def invalidate(self) -> None:
        """丢掉总表缓存。删除/重命名之后必须调，否则前端刷新看到的还是旧表。"""
        with self._lock:
            self._cache = None
            self._cache_key = None

    # ── 目录定位 ──

    @property
    def chats_root(self) -> Path:
        return (
            Path(settings.tavern_data_dir).expanduser()
            / settings.tavern_user
            / "chats"
        )

    def _card_file_stem(self, name: str) -> str:
        """卡名 → PNG 文件名（去掉后缀），即聊天目录名。

        走 catalog 而不是「拿卡名当文件名」：两者有 8 张卡对不上（见模块文档）。
        """
        for item in tavern_catalog.all_items():
            if item.type == TYPE_CHARACTER and item.name == name:
                return Path(item.file).stem
        # 否定路径上再强制扫一次：用户可能刚在酒馆里改了卡内嵌的名字，
        # 而改内容不动目录 mtime、缓存不会失效（同 catalog.exists 的处理）。
        for item in tavern_catalog.all_items(force=True):
            if item.type == TYPE_CHARACTER and item.name == name:
                return Path(item.file).stem
        raise TavernChatError(f"酒馆里找不到名为 “{name}” 的角色卡")

    def _chat_dir(self, name: str) -> Path:
        """某张卡的聊天目录，**并确保它没跑出 chats 根目录**。

        name 来自 HTTP 查询串。虽然它要先在 catalog 里匹配到才走得到这里
        （`../` 这类值根本匹配不上任何卡名），但路径校验不该依赖上游的
        副作用 —— 和 tavern_sync_service._entry_dir 一样，拼一次校验一次。
        """
        stem = self._card_file_stem(name)
        root = self.chats_root.resolve()
        target = (root / stem).resolve()
        if not target.is_relative_to(root) or target == root:
            raise TavernChatError("非法路径")
        return target

    def _chat_path(self, name: str, file_name: str) -> Path:
        """某张卡下的某个聊天文件。file_name 同样来自查询串，必须校验。"""
        base = self._chat_dir(name)
        if not file_name or not file_name.endswith(".jsonl"):
            raise TavernChatError("只能导出 .jsonl 聊天记录")
        target = (base / file_name).resolve()
        if not target.is_relative_to(base):
            raise TavernChatError("非法路径")
        if not target.is_file():
            raise TavernChatError("聊天记录不存在")
        return target

    # ── 列表 ──

    def list_chats(self, name: str) -> dict:
        """列出某张卡的全部聊天记录。

        卡不存在会抛错（用户打错名字该说清楚）；卡存在但没聊过只是
        `chats: []` —— 那是正常状态，不是错误。
        """
        chat_dir = self._chat_dir(name)
        rel = str(chat_dir.relative_to(self.chats_root.resolve()))
        if not chat_dir.is_dir():
            return {"name": name, "dir": rel, "exists": False, "chats": []}

        chats: list[ChatFile] = []
        for path in sorted(chat_dir.glob("*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            chats.append(
                ChatFile(
                    file=path.name,
                    size=st.st_size,
                    modified=_iso(st.st_mtime),
                    messages=_count_messages(path),
                )
            )
        # 最近聊的排前面 —— 要导出的基本都是刚聊完的那盘
        chats.sort(key=lambda c: c.modified, reverse=True)
        return {
            "name": name,
            "dir": rel,
            "exists": True,
            "chats": [c.as_dict() for c in chats],
        }

    def count_chats(self, name: str) -> int:
        """某张卡有几份聊天记录。管理列表里给个数字用，找不到卡就当 0。

        走 overview 的缓存而不是自己 glob：管理列表会对每一条调一次本方法，
        逐条各扫一次目录就是 O(条目数 × 目录数)。overview 一次扫完全部并缓存，
        这里只做一次字典查找。

        **刻意不触发强制重扫**：列表里「酒馆中缺失」的条目本就会被
        `catalog.exists()` 先重扫一遍，这里再来一次就是每条两次全量扫描 ——
        显示一个份数不值这个代价，缓存里查不到就当 0。
        """
        groups = self.overview().get("cards", [])
        for g in groups:
            if g["name"] == name:
                return len(g["chats"])
        return 0

    # ── 全量总表 ──

    def overview(self, *, force: bool = False) -> dict:
        """一次扫完所有聊天记录，供「按楼层排序 / 按卡分组 / 找出该删的」用。

        返回的每份记录都带 `user_messages`（我说了几句）—— 清理时真正的判据。
        实测 112 份、5620 行全量 JSON 解析 0.22 秒，比只数行贵一个数量级但
        绝对值仍很小，故不做增量，只按 mtime 缓存整体结果。

        `is_current` 标出酒馆当前会话（卡内嵌 `chat` 指针指向的那份）：
        这一份禁止删除/重命名，酒馆的自动保存会把它写回来。
        """
        key = self._mtime_key()
        with self._lock:
            if not force and self._cache is not None and self._cache_key == key:
                return self._cache
        # 扫描放锁外：同 catalog 的处理，别让并发请求排队等一次全量扫描
        data = self._scan_overview()
        with self._lock:
            self._cache = data
            self._cache_key = key
        return data

    def _mtime_key(self) -> tuple:
        """缓存键 = chats 根目录 + 每个子目录的 mtime。

        新增/删除/重命名聊天文件都会动所在子目录的 mtime；**聊天内容追加
        不动目录 mtime 但会动文件 mtime**，所以文件 mtime 也得进 key ——
        否则聊到一半刷新总表，楼层数纹丝不动。
        """
        root = self.chats_root
        try:
            parts: list = [root.stat().st_mtime_ns]
        except OSError:
            return ()
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            try:
                parts.append((sub.name, sub.stat().st_mtime_ns))
            except OSError:
                continue
            for f in sorted(sub.glob("*.jsonl")):
                try:
                    parts.append((f.name, f.stat().st_mtime_ns, f.stat().st_size))
                except OSError:
                    continue
        return tuple(parts)

    def _scan_overview(self) -> dict:
        root = self.chats_root
        if not root.is_dir():
            return {"cards": [], "totals": _totals([]), "chats_root": str(root)}

        # 目录名 → (卡名, 当前会话文件名)。目录名认的是 PNG 文件名（见模块文档），
        # 所以这里按 stem 建索引，而不是按卡名。
        index: dict[str, tuple[str, Optional[str]]] = {}
        for item in tavern_catalog.all_items():
            if item.type == TYPE_CHARACTER:
                index[Path(item.file).stem] = (item.name, item.chat)

        cards: list[dict] = []
        flat: list[dict] = []
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            card_name, current = index.get(sub.name, (sub.name, None))
            chats: list[dict] = []
            for path in sorted(sub.glob("*.jsonl")):
                try:
                    st = path.stat()
                except OSError:
                    continue
                total, user = _count_detail(path)
                row = {
                    "card": card_name,
                    "dir": sub.name,
                    "file": path.name,
                    "title": path.stem,
                    "size": st.st_size,
                    "modified": _iso(st.st_mtime),
                    "messages": total,
                    "user_messages": user,
                    # 酒馆当前会话：禁止删除/重命名的那一份
                    "is_current": current is not None and path.stem == current,
                }
                chats.append(row)
                flat.append(row)
            # 卡在酒馆里还在不在 —— 目录没被 index 命中就是卡已删除，
            # 此时聊天记录成了孤儿（实测本机为 0，但删卡后就会出现）
            cards.append(
                {
                    "name": card_name,
                    "dir": sub.name,
                    "orphan": sub.name not in index,
                    "chats": sorted(chats, key=lambda c: c["modified"], reverse=True),
                    "total_messages": sum(c["messages"] for c in chats),
                    "total_user_messages": sum(c["user_messages"] for c in chats),
                    "total_size": sum(c["size"] for c in chats),
                }
            )

        # 从未聊过的卡不进列表（282 张，铺出来只会淹没真正有内容的 95 张），
        # 但数量要报给前端，否则「95」这个数字没有参照系。
        never = sum(1 for stem in index if not (root / stem).is_dir())
        cards.sort(key=lambda c: c["name"])
        return {
            "cards": cards,
            "totals": _totals(flat) | {"cards_never_chatted": never},
            "chats_root": str(root),
        }

    # ── 导出 ──

    def export_one(self, name: str, file_name: str, fmt: str) -> tuple[str, bytes]:
        """导出一份聊天记录，返回 (下载文件名, 正文字节)。"""
        if fmt not in FORMATS:
            raise TavernChatError(f"未知格式: {fmt}")
        path = self._chat_path(name, file_name)
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise TavernChatError("聊天记录过大，请直接从酒馆目录取文件")

        stem = path.stem
        if fmt == "jsonl":
            # **按字节原样吐出**：jsonl 的用途是「拿回酒馆能直接导入」。
            # 走 read_text(errors="replace") 的话，文件里任何一个非法 UTF-8
            # 字节都会被悄悄换成 U+FFFD 写进下载文件 —— 用户拿到的是一份
            # 看起来正常、实则被改过的存档。原样搬运就没有这个问题。
            return f"{stem}.jsonl", path.read_bytes()

        return f"{stem}.{fmt}", _render(path, name, fmt).encode("utf-8")

    def export_all(self, name: str, fmt: str) -> tuple[str, bytes]:
        """把某张卡的全部聊天记录打包成 zip，返回 (下载文件名, zip 字节)。"""
        if fmt not in FORMATS:
            raise TavernChatError(f"未知格式: {fmt}")
        chat_dir = self._chat_dir(name)
        files = sorted(chat_dir.glob("*.jsonl")) if chat_dir.is_dir() else []
        if not files:
            raise TavernChatError(f"「{name}」还没有聊天记录")

        total = sum(p.stat().st_size for p in files)
        if total > _MAX_ZIP_BYTES:
            raise TavernChatError("聊天记录总量过大，请逐份导出")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                # 同 export_one：jsonl 走字节原样搬运，不做任何解码
                body = (
                    path.read_bytes()
                    if fmt == "jsonl"
                    else _render(path, name, fmt).encode("utf-8")
                )
                # zip 内不带目录层：解开就是一堆聊天文件，不用再进一级
                zf.writestr(f"{path.stem}.{fmt}", body)
        logger.info("导出聊天记录 %s: %d 份 (%s)", name, len(files), fmt)
        # zip 条目名用的是聊天文件名（可能含中文/空格），外层文件名用卡名，
        # 两者都由路由去做 HTTP 头的编码。
        return f"{_safe_stem(name)}-聊天记录-{fmt}.zip", buf.getvalue()

    def preview(self, name: str, file_name: str, limit: int = 60) -> dict:
        """弹窗预览用：只取前 limit 条，不把 5MB 正文塞进响应。"""
        path = self._chat_path(name, file_name)
        header, msgs = _read_chat(path)
        head = []
        for i, m in enumerate(msgs):
            if i >= limit:
                break
            head.append(
                {
                    "who": _speaker(m, header, name),
                    "is_user": bool(m.get("is_user")),
                    "at": _fmt_date(m.get("send_date")),
                    "text": m.get("mes") or "",
                }
            )
        return {
            "file": path.name,
            "total": len(msgs),
            "truncated": len(msgs) > len(head),
            "messages": head,
        }


# ── 解析与渲染 ──


def _count_messages(path: Path) -> int:
    """消息条数 = 行数 - 1（首行是 chat_metadata，不是消息）。

    按行数数而不是逐行 json.loads：最大的一份有 2 万条消息，全解一遍只为
    显示一个数字不划算，而 jsonl 的「一行一条」是酒馆自己写入时的硬约定。
    """
    try:
        with path.open("rb") as fh:
            lines = sum(1 for line in fh if line.strip())
    except OSError:
        return 0
    return max(0, lines - 1)


def _count_detail(path: Path) -> tuple[int, int]:
    """(消息条数, 其中我说的条数)。

    必须真解 JSON —— 「我说了几句」只能从 `is_user` 读出来，而它正是清理时
    最有用的判据（见模块文档）。坏行跳过，同 `_read_chat`。
    """
    total = 0
    user = 0
    seen = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                seen += 1
                # 首个非空行若没有 mes 就是元数据，不算消息（同 _read_chat 口径）
                if seen == 1 and "mes" not in obj:
                    continue
                total += 1
                if obj.get("is_user"):
                    user += 1
    except OSError:
        return 0, 0
    return total, user


def _totals(rows: list[dict]) -> dict:
    """总表的汇总数字。空列表也要给全字段，否则前端得到处判 undefined。"""
    return {
        "chats": len(rows),
        "cards_with_chats": len({r["dir"] for r in rows}),
        "messages": sum(r["messages"] for r in rows),
        "user_messages": sum(r["user_messages"] for r in rows),
        "size": sum(r["size"] for r in rows),
        # 「我一句话都没说过」的份数 —— 清理的首要目标
        "never_replied": sum(1 for r in rows if r["user_messages"] == 0),
    }


def _read_chat(path: Path) -> tuple[dict, list[dict]]:
    """读成 (首行元数据, 消息列表)。

    坏行**跳过而不是抛错**：一份 2 万条的记录里有一行写坏（酒馆崩在写入
    中途就会），不该让整份记录导不出来。
    """
    header: dict = {}
    msgs: list[dict] = []
    seen = 0  # 已接受的非空行数 —— 不能用 enumerate 的原始行号
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("跳过坏行 %s:%d", path.name, idx + 1)
                continue
            if not isinstance(obj, dict):
                continue
            seen += 1
            # 元数据是第一个**非空**行（有 chat_metadata / user_name，没有 mes）。
            # 这里若按原始行号 idx==0 判断，文件开头多一个空行就会让元数据行
            # 落进消息列表 —— 它没有 mes，导出时会多出一条空消息，
            # 且与 _count_messages（数非空行）的口径对不上。
            if seen == 1 and "mes" not in obj:
                header = obj
                continue
            msgs.append(obj)
    return header, msgs


def _speaker(msg: dict, header: dict, card_name: str) -> str:
    """这条消息是谁说的。

    优先用消息自带的 name（酒馆每条都写，群聊里不同角色靠它区分）；
    缺失时才回退到首行的 user_name / character_name。注意首行的这两个字段
    实测常常是占位值 `unused`，所以不能反过来优先用它们。
    """
    name = msg.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != "unused":
        return name.strip()
    if msg.get("is_user"):
        who = header.get("user_name")
    else:
        who = header.get("character_name") or card_name
    if isinstance(who, str) and who.strip() and who.strip() != "unused":
        return who.strip()
    return "User" if msg.get("is_user") else card_name


def _fmt_date(raw) -> str:
    """send_date 有两种写法：ISO 串（新）和毫秒时间戳（旧）。都转成可读串。"""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            # 酒馆早期版本写的是 "2026-3-2 @08h 53m 30s" 这种本地化串，
            # 解不动就原样留着 —— 有个大概时间比没有强。
            return raw.strip()
    return ""


def _render(path: Path, card_name: str, fmt: str) -> str:
    header, msgs = _read_chat(path)
    lines: list[str] = []
    if fmt == "md":
        lines.append(f"# {card_name} — {path.stem}")
        lines.append("")
        lines.append(f"> 共 {len(msgs)} 条消息 · 导出自 SillyTavern")
        lines.append("")
        lines.append("---")
        lines.append("")
        for m in msgs:
            who = _speaker(m, header, card_name)
            at = _fmt_date(m.get("send_date"))
            mark = "🧑" if m.get("is_user") else "🤖"
            lines.append(f"### {mark} {who}" + (f"  `{at}`" if at else ""))
            lines.append("")
            lines.append(_body(m))
            lines.append("")
    else:  # txt
        lines.append(f"{card_name} — {path.stem}")
        lines.append(f"共 {len(msgs)} 条消息")
        lines.append("=" * 50)
        lines.append("")
        for m in msgs:
            who = _speaker(m, header, card_name)
            at = _fmt_date(m.get("send_date"))
            lines.append(f"【{who}】" + (f" {at}" if at else ""))
            lines.append(_body(m))
            lines.append("-" * 50)
    return "\n".join(lines).rstrip() + "\n"


def _body(msg: dict) -> str:
    """消息正文。

    `mes` 就是当前选中的 swipe（见模块文档），直接用。
    只做一件清理：把 `\\r\\n` 归一成 `\\n` —— 酒馆写入的正文里两种换行混用，
    落到 md 文件里会在某些编辑器中显示成多余空行。
    """
    text = msg.get("mes")
    if not isinstance(text, str):
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _safe_stem(name: str) -> str:
    """把卡名压成能当文件名的样子（下载名用）。

    只替换路径分隔符与控制字符，不做更激进的清洗 —— 中文、空格、括号在
    现代文件系统里都合法，替掉只会让用户认不出是哪张卡。
    """
    out = []
    for ch in name:
        out.append("_" if (ch in '/\\:*?"<>|' or ord(ch) < 32) else ch)
    cleaned = "".join(out).strip().strip(".")
    return cleaned or "chat"


tavern_chats = TavernChatService()

__all__ = ["tavern_chats", "TavernChatError", "FORMATS"]
