"""扫描 SillyTavern 的 data 目录，回答「酒馆里有哪些角色卡 / 世界书 / 预设」。

**只读**。写操作一律走 tavern_sync（见 tavern_sync_service），本模块绝不碰
酒馆的数据文件 —— 酒馆运行时持有这些文件，越过它写等于和它抢。

为什么角色卡不能只看文件名：酒馆认的是**卡内嵌的 name 字段**，不是 PNG 的
文件名。实测 376 张卡里有 8 张两者不一致（`default_Seraphina.png` 的卡名是
`Seraphina`、`太吾绘卷.png` 的卡名是 `太吾绘卷v1.4`）。tavern_sync 的
`酒馆中的名称` 走的是前者，按文件名填进去会得到「酒馆里找不到这张卡」。
故必须解 PNG 的 tEXt 块。全量扫 376 张约 0.65 秒，配合 mtime 缓存后
只有目录真变了才重扫。
"""

from __future__ import annotations

import base64
import json
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from app.core.logging import get_logger
from app.core.settings import settings

logger = get_logger("tavern_catalog")

# tavern_sync 支持的三种类型。值同时用作 tavern_sync.yaml 里的「类型」字段，
# 所以这里的字符串不能随意改名。
TYPE_CHARACTER = "角色卡"
TYPE_WORLDBOOK = "世界书"
TYPE_PRESET = "预设"
ALL_TYPES = (TYPE_CHARACTER, TYPE_WORLDBOOK, TYPE_PRESET)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 卡片元数据的 tEXt 关键字。ccv3 是 Character Card V3，chara 是老格式；
# 两者都可能存在，V3 优先（它带的字段更全，且新卡只写 V3）。
_CHARA_KEYS = (b"ccv3", b"chara")


@dataclass(frozen=True)
class TavernItem:
    """酒馆中的一份资料。

    name   —— 酒馆认的名称，也是填进 tavern_sync.yaml 的「酒馆中的名称」
    type   —— 角色卡 / 世界书 / 预设
    file   —— 源文件名（仅供展示与排障，不参与同步）
    chat   —— 仅角色卡有：卡内嵌的「当前会话」文件名（不含 .jsonl）。
              酒馆打开这张卡时默认载入它。聊天记录管理据此标出「正在用」的
              那一份 —— 动它需要先在酒馆里切走，否则酒馆的自动保存会把
              删掉的文件重新写回来。
    """

    name: str
    type: str
    file: str
    chat: Optional[str] = None

    def as_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "file": self.file}


def _read_png_card_meta(path: Path) -> tuple[Optional[str], Optional[str]]:
    """从角色卡 PNG 的 tEXt 块里取出 (卡片名, 当前会话文件名)。

    逐块跳读而不是整文件读入：卡的图可以有几 MB，而元数据块通常在前部，
    按块走能在命中后立刻停手。解析失败一律返回 (None, None)（交给调用方
    回退到文件名），单张坏卡不该让整个列表挂掉。
    """
    try:
        with path.open("rb") as fh:
            if fh.read(8) != _PNG_MAGIC:
                return None, None
            found: dict[bytes, tuple[Optional[str], Optional[str]]] = {}
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    break
                length = struct.unpack(">I", header[:4])[0]
                chunk_type = header[4:8]
                if chunk_type == b"IEND":
                    break
                if chunk_type == b"tEXt":
                    payload = fh.read(length)
                    fh.read(4)  # CRC
                    keyword, _, value = payload.partition(b"\x00")
                    key = keyword.lower()
                    if key in _CHARA_KEYS and key not in found:
                        meta = _decode_card_meta(value)
                        if meta[0]:
                            found[key] = meta
                else:
                    # 数据段 + 4 字节 CRC
                    fh.seek(length + 4, 1)
            for key in _CHARA_KEYS:
                if key in found:
                    return found[key]
    except (OSError, struct.error) as exc:
        logger.debug("读取角色卡失败 %s: %s", path.name, exc)
    return None, None


def _decode_card_meta(value: bytes) -> tuple[Optional[str], Optional[str]]:
    try:
        data = json.loads(base64.b64decode(value))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    # V2/V3 把正文塞在 data 里，老格式是平铺的
    inner = data.get("data")
    holder = inner if isinstance(inner, dict) else data
    name = holder.get("name") or data.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    # chat 指针在老格式里是平铺的（V3 的 data 里通常没有），两边都试
    chat = holder.get("chat") or data.get("chat")
    chat = chat.strip() if isinstance(chat, str) and chat.strip() else None
    return name, chat


class TavernCatalog:
    """酒馆资料目录的只读视图，带 mtime 缓存。

    缓存键是三个目录的 mtime 组合。加卡 / 删卡 / 改名都会动目录 mtime，
    因此「目录没变就不重扫」是安全的；卡**内容**被改写（换名但不换文件名）
    不会动目录 mtime，这种情况靠前端的「刷新」按钮显式重扫。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[list[TavernItem]] = None
        self._cache_key: Optional[tuple] = None

    # ── 目录定位 ──

    @property
    def user_dir(self) -> Path:
        return Path(settings.tavern_data_dir).expanduser() / settings.tavern_user

    def _dirs(self) -> dict[str, Path]:
        base = self.user_dir
        return {
            TYPE_CHARACTER: base / "characters",
            TYPE_WORLDBOOK: base / "worlds",
            # 预设目录名带空格，是酒馆定的，不是笔误
            TYPE_PRESET: base / "OpenAI Settings",
        }

    def availability(self) -> dict:
        """各类型目录是否存在 —— 前端用它区分「一个都没有」和「路径配错了」。"""
        out = {"user_dir": str(self.user_dir), "types": {}}
        for type_name, path in self._dirs().items():
            out["types"][type_name] = {"path": str(path), "exists": path.is_dir()}
        return out

    # ── 扫描 ──

    def _mtime_key(self) -> tuple:
        key = []
        for type_name, path in sorted(self._dirs().items()):
            try:
                key.append((type_name, path.stat().st_mtime_ns))
            except OSError:
                key.append((type_name, None))
        return tuple(key)

    def _scan(self) -> list[TavernItem]:
        items: list[TavernItem] = []
        dirs = self._dirs()

        char_dir = dirs[TYPE_CHARACTER]
        if char_dir.is_dir():
            for png in sorted(char_dir.glob("*.png")):
                card_name, chat = _read_png_card_meta(png)
                items.append(
                    TavernItem(
                        name=card_name or png.stem,
                        type=TYPE_CHARACTER,
                        file=png.name,
                        chat=chat,
                    )
                )

        for type_name in (TYPE_WORLDBOOK, TYPE_PRESET):
            folder = dirs[type_name]
            if not folder.is_dir():
                continue
            for jf in sorted(folder.glob("*.json")):
                # 世界书与预设的「酒馆中的名称」就是去掉 .json 的文件名 ——
                # 酒馆自己也是这么认的（它们的 JSON 内部不存名字）。
                items.append(TavernItem(name=jf.stem, type=type_name, file=jf.name))

        return items

    def all_items(self, *, force: bool = False) -> list[TavernItem]:
        key = self._mtime_key()
        with self._lock:
            if not force and self._cache is not None and self._cache_key == key:
                return self._cache
        # 扫描放在锁外：它有 I/O，别让并发请求排队等一个 0.6 秒的扫描；
        # 重复扫一次的代价远小于把请求串行化。
        items = self._scan()
        with self._lock:
            self._cache = items
            self._cache_key = key
        logger.info("扫描酒馆资料完成: %d 项", len(items))
        return items

    def counts(self, *, force: bool = False) -> dict[str, int]:
        out = {t: 0 for t in ALL_TYPES}
        for item in self.all_items(force=force):
            out[item.type] = out.get(item.type, 0) + 1
        return out

    # ── 搜索 ──

    def search(
        self,
        query: str,
        *,
        types: Optional[Iterable[str]] = None,
        limit: int = 30,
        force: bool = False,
    ) -> list[dict]:
        """按名称模糊搜索。

        **刻意不支持「列出全部」**：391 张卡 + 218 本世界书全铺出来对人没用，
        用户明确要的是搜出来再加。空查询返回空列表，由前端提示先输入关键词。

        排序：前缀命中 > 子串命中，同档按名称长度（短的通常是用户要的那个），
        再按名称本身保证结果稳定。
        """
        q = (query or "").strip().lower()
        if not q:
            return []

        wanted = set(types) if types else set(ALL_TYPES)
        scored: list[tuple[int, int, str, TavernItem]] = []
        for item in self.all_items(force=force):
            if item.type not in wanted:
                continue
            low = item.name.lower()
            if low == q:
                rank = 0
            elif low.startswith(q):
                rank = 1
            elif q in low:
                rank = 2
            else:
                continue
            scored.append((rank, len(item.name), item.name, item))

        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        return [item.as_dict() for *_rest, item in scored[: max(1, limit)]]

    def exists(self, name: str, type_name: str) -> bool:
        """酒馆里是否真有这个名字的这类资料 —— 加管理项前用它挡住手滑打错。

        没命中时**强制重扫一次再判**：目录 mtime 变了缓存本会自动失效，但卡片
        内容被改写（换了内嵌 name 却没换文件名）不动目录 mtime。不补这一下的话，
        用户刚在酒馆里改完名，这里会一口咬定「酒馆里没有」，而搜索又能搜到，
        前后矛盾且无从下手。只在否定路径上多扫一次，正常添加不受影响。
        """

        def _hit(items: list[TavernItem]) -> bool:
            return any(i.name == name and i.type == type_name for i in items)

        if _hit(self.all_items()):
            return True
        return _hit(self.all_items(force=True))


tavern_catalog = TavernCatalog()
