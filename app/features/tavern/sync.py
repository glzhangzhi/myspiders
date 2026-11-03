"""管理 tavern_sync.yaml 里的「要同步哪些资料」，并驱动 pull / push。

职责边界：
  - 本模块**只**改 tavern_sync.yaml 的「配置」区，以及调用 ts.mjs
  - 酒馆里有什么 → tavern_catalog（只读扫描）
  - 拉下来的 yaml 长什么样 → tavern_sync 自己写，本模块只报告文件树

两个必须守住的不变式：

1. **YAML 写入与 pull/push 共用一把锁。**
   ts.mjs 跑 pull/push 时会把展开后的 YAML **临时写回原文件**，结束再还原
   （见 ts.mjs 的 withCompletedConfig）。此时如果仪表盘正好写入一条新配置，
   那次还原会把它整个抹掉 —— 用户看到的是「加了但没保存上」。故两者串行。

2. **一次只跑一个 pull/push。**
   tavern_sync 固定监听 6620 端口等酒馆网页连上来，第二个进程起来必然
   EADDRINUSE。与其让用户看一堆端口报错，不如在这里就挡住并说清楚。

写回策略：完整写法的配置块**原样保留**（用户可能手工加了导出文件路径之类的
字段），只有分组简写那部分由本模块重新生成。
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.tavern.catalog import ALL_TYPES, tavern_catalog

logger = get_logger("tavern_sync")

# 一把锁管两件事：改 YAML 与跑 ts.mjs（见模块文档的不变式 1、2）
_sync_lock = threading.Lock()

_DEFAULT_HEADER = (
    "# yaml-language-server: "
    "$schema=https://testingcf.jsdelivr.net/gh/StageDog/tavern_sync/dist/schema/settings.zh.json\n"
)


class TavernSyncError(RuntimeError):
    """预期内的用户可读错误（配置重名、酒馆里查无此名等）。"""


# pull/push 失败里出现频率最高的一种，值得给一句能照着做的话
_NOT_CONNECTED_MSG = (
    "酒馆网页没有连上来。tavern_sync 需要网页端的「实时编写」脚本"
    "（酒馆助手 → 脚本库）连到本机 6620 端口才能读写数据。"
    "请打开酒馆页面、确认该脚本已启用，然后重试"
)


@dataclass
class ConfigEntry:
    """tavern_sync.yaml「配置」区里的一条。

    kind='short' 是分组简写（由本模块生成/维护）；
    kind='full'  是完整写法，raw 里存着原始行，写回时原样吐出去。
    """

    name: str
    type: str
    alias: Optional[str] = None
    kind: str = "short"
    raw: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "alias": self.alias,
            "kind": self.kind,
        }


# ── YAML 标量：与 ts.mjs 的 yamlScalar 保持同一套判据 ──
_UNSAFE = re.compile(r"""[\[\]{}:,&*?|>!'"%@#`]""")


def _yaml_scalar(text: str) -> str:
    if (
        _UNSAFE.search(text)
        or re.match(r"^[\s-]", text)
        or re.search(r"\s$", text)
        or text == ""
    ):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _unquote(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and ((t[0] == t[-1] == '"') or (t[0] == t[-1] == "'")):
        return t[1:-1]
    return t


class TavernSyncService:
    # ── 路径 ──

    @property
    def root(self) -> Path:
        return Path(settings.tavern_sync_dir).expanduser()

    @property
    def yaml_path(self) -> Path:
        return self.root / "tavern_sync.yaml"

    @property
    def entry_script(self) -> Path:
        return self.root / settings.tavern_sync_entry

    # ── 解析 ──

    def _read_text(self) -> str:
        if not self.yaml_path.exists():
            return _DEFAULT_HEADER + "\n配置:\n"
        return self.yaml_path.read_text(encoding="utf-8")

    def _parse(self, content: str) -> tuple[list[str], list[ConfigEntry], list[str]]:
        """拆成 (配置区之前的行, 条目, 配置区之后的行)。

        行的归属按 tavern_sync.yaml 的缩进约定判断：`配置:` 顶格，条目缩进 2 格，
        字段缩进 4 格；再次出现顶格非空行就说明配置区结束了。
        """
        lines = content.split("\n")
        header: list[str] = []
        trailing: list[str] = []
        entries: list[ConfigEntry] = []

        in_config = False
        seen_config = False
        group_type: Optional[str] = None
        current: Optional[ConfigEntry] = None

        for i, line in enumerate(lines):
            if not seen_config:
                if re.match(r"^配置\s*:", line):
                    seen_config = True
                    in_config = True
                    continue
                header.append(line)
                continue

            if not in_config:
                trailing.append(line)
                continue

            # 顶格非空 → 离开配置区
            if line.strip() and not line[0].isspace():
                in_config = False
                current = None
                group_type = None
                trailing.append(line)
                continue

            if not line.strip() or re.match(r"^\s*#", line):
                # 配置区内的空行/整行注释：跟着当前完整写法块走，否则丢弃
                if current is not None and current.kind == "full":
                    current.raw.append(line)
                continue

            group = self._match_group_header(lines, i)
            if group:
                group_type = group
                current = None
                continue

            item = self._match_list_item(line)
            if item is not None:
                name, alias = item
                if not group_type:
                    raise TavernSyncError(
                        f"tavern_sync.yaml 第 {i + 1} 行: 列表项 “{name}” 不在任何类型分组下"
                    )
                entries.append(
                    ConfigEntry(name=name, type=group_type, alias=alias, kind="short")
                )
                current = None
                continue

            head = re.match(r"^  ([^#\s-].*?):\s*(?:#\s*(.+?))?\s*$", line)
            if head:
                current = ConfigEntry(
                    name=_unquote(head.group(1)),
                    type="",
                    alias=(head.group(2) or "").strip() or None,
                    kind="full",
                    raw=[line],
                )
                entries.append(current)
                group_type = None
                continue

            if current is not None and current.kind == "full":
                current.raw.append(line)
                type_match = re.match(r"^    类型:\s*(.+?)\s*$", line)
                if type_match:
                    current.type = _unquote(re.sub(r"\s+#.*$", "", type_match.group(1)))

        # header 原样保留（含末尾空行）—— 重排它会让每次写入都产生无谓的 diff。
        # trailing 只掐掉两端空行，写回时由 _render 统一补一个间隔。
        while trailing and not trailing[0].strip():
            trailing.pop(0)
        while trailing and not trailing[-1].strip():
            trailing.pop()

        return header, entries, trailing

    @staticmethod
    def _match_group_header(lines: list[str], i: int) -> Optional[str]:
        """第 i 行是不是分组标题（如 `  角色卡:`）。

        必须确认后面紧跟列表项才算 —— 否则一个碰巧叫「角色卡」的完整写法配置
        会被误判成分组。
        """
        m = re.match(r"^\s+([^#\s].*?):\s*(?:#.*)?$", lines[i])
        if not m:
            return None
        type_name = _unquote(m.group(1))
        if type_name not in ALL_TYPES:
            return None
        for nxt in lines[i + 1 :]:
            if not nxt.strip() or re.match(r"^\s*#", nxt):
                continue
            return type_name if re.match(r"^\s*-\s+\S", nxt) else None
        return None

    @staticmethod
    def _match_list_item(line: str) -> Optional[tuple[str, Optional[str]]]:
        """解析 `    - 名字  # 别名`。

        按 YAML 规则 `#` 前必须有空白才是注释，所以「名字#不是注释」整体算名称。
        """
        m = re.match(r"^\s*-\s+(\S.*?)\s*$", line)
        if not m:
            return None
        rest = m.group(1)

        if rest[0] in ('"', "'"):
            quote = rest[0]
            end = rest.find(quote, 1)
            if end == -1:
                return None
            after = rest[end + 1 :].strip()
            alias = after[1:].strip() or None if after.startswith("#") else None
            return rest[1:end], alias

        hash_match = re.search(r"\s#", rest)
        if not hash_match:
            return rest.strip(), None
        name = rest[: hash_match.start()].strip()
        alias = re.sub(r"^#\s*", "", rest[hash_match.start() :].strip()) or None
        return name, alias

    # ── 序列化 ──

    def _render(
        self, header: list[str], entries: list[ConfigEntry], trailing: list[str]
    ) -> str:
        out: list[str] = []
        out.extend(header)
        # header 末尾没有空行时补一个，让 `配置:` 与上方说明拉开距离
        if out and out[-1].strip():
            out.append("")
        out.append("配置:")

        # 分组简写：按固定类型顺序输出，同类内保持加入顺序
        for type_name in ALL_TYPES:
            group = [e for e in entries if e.kind == "short" and e.type == type_name]
            if not group:
                continue
            out.append(f"  {type_name}:")
            for entry in group:
                line = f"    - {_yaml_scalar(entry.name)}"
                if entry.alias:
                    line += f"  # {entry.alias}"
                out.append(line)

        # 完整写法原样保留
        for entry in entries:
            if entry.kind == "full":
                out.extend(entry.raw)

        if trailing:
            out.append("")
            out.extend(trailing)

        text = "\n".join(out).rstrip("\n") + "\n"
        return text

    # ── 查询 ──

    def list_entries(self) -> list[dict]:
        """当前被管理的配置，附带本地文件是否已经拉下来。"""
        _header, entries, _trailing = self._parse(self._read_text())
        result = []
        for entry in entries:
            data = entry.as_dict()
            data.update(self._local_state(entry))
            data["in_tavern"] = tavern_catalog.exists(entry.name, entry.type)
            result.append(data)
        return result

    def _entry_dir(self, entry_name: str, entry_type: str) -> Path:
        """配置在本地的目录，**并确保它没跑出同步根目录**。

        entry_name / entry_type 都来自 HTTP 查询串，直接 `root / type / name`
        拼出来的路径可以被 `../` 抬到任意位置 —— 实测 name=`../../../../etc`
        能读到 /etc/passwd。凡是拿这两个值拼路径的地方一律走这里。
        """
        root = self.root.resolve()
        target = (root / entry_type / entry_name).resolve()
        if not target.is_relative_to(root) or target == root:
            raise TavernSyncError("非法路径")
        return target

    def _local_state(self, entry: ConfigEntry) -> dict:
        """这条配置在本地落成了什么。

        分组简写的路径由 ts.mjs 按 `{类型}/{配置名}/目录` 生成；完整写法可能被
        用户改过路径，这里不去猜，只报告默认位置是否存在。
        """
        rel_dir = Path(entry.type) / entry.name
        try:
            abs_dir = self._entry_dir(entry.name, entry.type)
        except TavernSyncError:
            # YAML 里手写了越界名称：只报告成「没拉取」，不让它炸掉整个列表
            return {"local_dir": str(rel_dir), "pulled": False, "file_count": 0}
        index = abs_dir / "目录.yaml"
        files = 0
        if abs_dir.is_dir():
            files = sum(1 for p in abs_dir.rglob("*") if p.is_file())
        return {
            "local_dir": str(rel_dir),
            "pulled": index.exists(),
            "file_count": files,
        }

    def local_tree(self, entry_name: str, entry_type: str, *, max_files: int = 400) -> dict:
        """列出某条配置在本地的文件（供仪表盘展示资料结构）。"""
        rel_dir = Path(entry_type) / entry_name
        abs_dir = self._entry_dir(entry_name, entry_type)
        if not abs_dir.is_dir():
            return {"dir": str(rel_dir), "exists": False, "files": []}
        files = []
        for path in sorted(abs_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append(
                {"path": str(path.relative_to(abs_dir)), "size": size}
            )
            if len(files) >= max_files:
                break
        return {"dir": str(rel_dir), "exists": True, "files": files}

    def read_local_file(self, entry_name: str, entry_type: str, rel_path: str) -> str:
        """读取本地某个文件的内容（只读、限定在该配置目录内）。

        name / type / path **三者都**要校验：早先只挡了 rel_path，结果
        name=`../../../../etc` + path=`passwd` 能读到 /etc/passwd。
        """
        base = self._entry_dir(entry_name, entry_type)
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            raise TavernSyncError("非法路径")
        if not target.is_file():
            raise TavernSyncError("文件不存在")
        if target.stat().st_size > 2_000_000:
            raise TavernSyncError("文件过大，请直接在本地打开")
        return target.read_text(encoding="utf-8", errors="replace")

    # ── 增删 ──

    def add_entry(self, name: str, type_name: str, alias: Optional[str] = None) -> dict:
        name = (name or "").strip()
        alias = (alias or "").strip() or None
        if not name:
            raise TavernSyncError("名称不能为空")
        if type_name not in ALL_TYPES:
            raise TavernSyncError(f"未知类型: {type_name}")
        # alias 是原样拼进 `# 别名` 注释的，换行会把后面的内容顶成新的 YAML 行
        # （实测 alias="X\n  恶意: 注入" 会在配置区里凭空多出一个顶层键）
        if alias and not re.fullmatch(r"[^\s#][^\r\n#]*", alias):
            raise TavernSyncError("别名不能包含换行或 #，且不能以空白开头")
        if not tavern_catalog.exists(name, type_name):
            raise TavernSyncError(
                f"酒馆里找不到名为 “{name}” 的{type_name}。"
                "请用搜索结果里的名字（角色卡认的是卡内嵌的名称，可能与文件名不同）"
            )

        with _sync_lock:
            header, entries, trailing = self._parse(self._read_text())
            # 重名会让展开后的 YAML 出现重复键，YAML 只保留最后一个 —— 必须拦住
            for existing in entries:
                if existing.name == name:
                    raise TavernSyncError(f"“{name}” 已在管理列表中")
            if alias and any(e.alias == alias for e in entries):
                raise TavernSyncError(f"别名 “{alias}” 已被占用")
            entries.append(
                ConfigEntry(name=name, type=type_name, alias=alias, kind="short")
            )
            self._write(header, entries, trailing)
        logger.info("已添加管理项: %s (%s)", name, type_name)
        return {"name": name, "type": type_name, "alias": alias}

    def remove_entry(self, name: str, *, delete_local: bool = False) -> dict:
        with _sync_lock:
            header, entries, trailing = self._parse(self._read_text())
            target = next((e for e in entries if e.name == name), None)
            if target is None:
                raise TavernSyncError(f"“{name}” 不在管理列表中")
            entries = [e for e in entries if e.name != name]
            self._write(header, entries, trailing)

            removed_local = False
            if delete_local:
                removed_local = self._delete_local_dir(target)

        logger.info("已移除管理项: %s (本地目录删除=%s)", name, removed_local)
        return {"name": name, "removed_local": removed_local}

    def _delete_local_dir(self, entry: ConfigEntry) -> bool:
        """删掉这条配置在本地的目录。

        只删 `{类型}/{配置名}/` 这个默认位置，且必须真的落在同步根目录之内 ——
        配置名来自用户输入，不做这层校验的话一个 `../` 就能删到别处去。
        """
        import shutil

        try:
            target = self._entry_dir(entry.name, entry.type)
        except TavernSyncError:
            logger.warning("拒绝删除越界目录: %s/%s", entry.type, entry.name)
            return False
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    def _write(
        self, header: list[str], entries: list[ConfigEntry], trailing: list[str]
    ) -> None:
        """原子写：先写临时文件再 rename。

        直接覆写的话，进程在写一半时挂掉会留下半个 YAML —— 那意味着 pull/push
        全部报解析错，而用户完全不知道自己的管理列表已经烂了。
        """
        text = self._render(header, entries, trailing)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.yaml_path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.yaml_path)

    # ── 执行 pull / push ──

    def run_sync(
        self, action: str, name: Optional[str] = None, *, force: bool = False
    ) -> dict:
        if action not in ("pull", "push"):
            raise TavernSyncError(f"不支持的操作: {action}")
        if not self.entry_script.exists():
            raise TavernSyncError(f"找不到同步脚本: {self.entry_script}")

        target = (name or "").strip() or "all"
        cmd = ["node", str(self.entry_script), action, target]
        if force:
            cmd.append("--force")

        # 非阻塞抢锁：6620 端口独占，排队等只会让第二个请求干等到超时
        if not _sync_lock.acquire(blocking=False):
            raise TavernSyncError("已有同步任务正在运行，请等它结束后再试")

        # 超时要收拾两样东西，缺一样下一次同步就起不来：
        #   1. ts.mjs 是**包装器**，真正占着 6620 端口的是它 spawn 的
        #      tavern_sync.mjs。只杀 ts.mjs 会留下一个孤儿进程霸着端口，
        #      之后每次 pull/push 都 EADDRINUSE（实测确认）。
        #      故整个进程组一起杀 —— start_new_session 让子进程自成一组。
        #   2. ts.mjs 跑之前把展开后的完整写法**写回了原文件**，本该在退出时
        #      还原。被 SIGKILL 打断的话还原不会发生，用户的 YAML 就永久停在
        #      展开态（管理列表还在，但简写全没了）。故这里自己备份 + 还原。
        yaml_backup = self._read_text() if self.yaml_path.exists() else None
        try:
            logger.info("执行: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            collected, reason = self._watch(proc)
            if reason is not None:
                self._kill_group(proc)
                if yaml_backup is not None:
                    self.yaml_path.write_text(yaml_backup, encoding="utf-8")
                raise TavernSyncError(reason)
            code = proc.returncode
            stdout, stderr = "".join(collected), ""
        finally:
            _sync_lock.release()

        output = _strip_ansi((stdout or "") + ("\n" + stderr if stderr else ""))
        # 只认退出码。曾经顺带扫输出里的「失败」「Error」字样，但那两个词会出现在
        # **资料名称**里（tavern_sync 的成功信息会回显名称），一张叫「拉取失败演练」
        # 的卡拉成功了也会被判成失败。exit_code 是 tavern_sync 的正式结论。
        ok = code == 0
        return {
            "ok": ok,
            "action": action,
            "target": target,
            "exit_code": code,
            "output": output.strip(),
        }

    @staticmethod
    def _watch(proc: subprocess.Popen) -> tuple[list[str], Optional[str]]:
        """边读输出边等进程结束，返回 (已收集的输出, 需要中止的原因或 None)。

        为什么不直接 communicate(timeout=...)：等满 180 秒才报「酒馆网页没开」
        对用户太难受，而这个失败其实**几秒内就能确定**。tavern_sync 启动后先打印
        「正运行在端口: 6620, 请打开酒馆网页」，网页连上时再打印「成功连接到酒馆网页」。
        只要若干秒内没看到后者，就能立刻断定网页没开，不必干等到总超时。

        读输出必须**另起线程**：脚本在等连接时一个字节都不再输出，主线程若卡在
        readline 上就永远走不到超时判断（实测会一直挂着）。线程设为 daemon，
        进程被杀后它自己随管道 EOF 结束。

        连上之后不再卡连接时限（大卡拉取本来就慢），只留总超时兜底。
        """
        import queue
        import threading
        import time

        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.put(line)
            except (ValueError, OSError):
                pass
            finally:
                lines.put(None)  # EOF 哨兵

        threading.Thread(target=reader, daemon=True).start()

        collected: list[str] = []
        connected = False
        eof = False
        started = time.monotonic()
        deadline = started + settings.tavern_sync_timeout
        # 网页真开着时 socket.io 握手走本机回环，一两秒就连上。给 15 秒足够宽松。
        connect_deadline = started + 15

        while True:
            now = time.monotonic()
            if now > deadline:
                return collected, (
                    f"同步超时（{settings.tavern_sync_timeout} 秒）。"
                    + (
                        "酒馆网页已连上但操作没能在时限内完成，请查看酒馆页面的控制台"
                        if connected
                        else _NOT_CONNECTED_MSG
                    )
                )
            if not connected and now > connect_deadline:
                return collected, _NOT_CONNECTED_MSG

            if eof:
                # 输出已结束，剩下只等进程收尾
                try:
                    proc.wait(timeout=max(1.0, deadline - now))
                    return collected, None
                except subprocess.TimeoutExpired:
                    continue

            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                continue
            if line is None:
                eof = True
                continue
            collected.append(line)
            if "成功连接到酒馆网页" in line:
                connected = True

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """连同子进程一起终止，先 TERM 后 KILL。

        先给 SIGTERM：tavern_sync 收到后会自己关掉 socket 服务器，比直接 KILL
        更干净。留 5 秒善后，还没退再 SIGKILL 兜底。
        """
        import os
        import signal

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except OSError:
                return
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue
        logger.warning("同步进程组 %s 未能终止", pgid)

    def status(self) -> dict:
        """同步环境是否就绪 —— 前端用它在出错前先给出人话提示。"""
        return {
            "sync_dir": str(self.root),
            "yaml_path": str(self.yaml_path),
            "yaml_exists": self.yaml_path.exists(),
            "script_exists": self.entry_script.exists(),
            "busy": _sync_lock.locked(),
            "timeout": settings.tavern_sync_timeout,
        }


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """ts.mjs / tavern_sync 输出带颜色转义，丢进 HTML 是一堆乱码。"""
    return _ANSI.sub("", text)


tavern_sync_service = TavernSyncService()
