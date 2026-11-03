"""统一 LLM 用量账本：三个来源 → Mongo `llm_usage`（一次调用一条）。

## 三个来源（都用机主同一个 DeepSeek 账号，或将要换成它）

| source | 谁写 | 在哪 |
|---|---|---|
| `dsh`    | DSH 插件 `dsh-usage-ledger`（挂 `llm/stream`，见 /root/dsh-usage-ledger） | `~/.dsh/usage-ledger/YYYY-MM.jsonl` + `backfill.jsonl` |
| `tavern` | st-proxy（**只**给酒馆用，2026-11-02 起） | `/root/st-proxy/ledger/YYYY-MM.jsonl` |
| `liyuan` | 梨园自己的会话文件（2026-11-02 起直连 DeepSeek，不再经代理） | `~/.liyuan/agent/sessions/**.jsonl` |

前两者是同一种 v1 账本行（`{v,source,id,ts(epoch ms UTC),provider,model,miss,hit,
cacheWrite,output,reasoning,...}`）；梨园沿用 `liyuan_usage.parse_session_file()`
（它已处理「轮末镜像」重复计数），再映射成同样的文档。

## 幂等与增量

- 账本文件是**追加写**的：每个文件记一个**字节偏移水位**（`llm_usage_state`），
  只读新增部分。**结尾没有换行的半行不推进水位** —— 写入方可能正写到一半，
  推进了就永远丢掉这一行（不报错、只是少一笔）。文件变短（被替换，如重跑回填）
  则从 0 重读，靠 `id` 唯一索引兜住重复。
- 梨园会话文件不大，每轮全量重解析，按 `id` upsert。
- 主键 `id`：dsh = 插件生成的 uuid / 回填的 `sessionId:seq`；tavern = DeepSeek 响应 id；
  liyuan = DeepSeek `responseId`（缺失时 `liyuan:<session>:<seq>`）。

## 跨来源去重

st-proxy 在 2026-11-02 之前同时服务酒馆和梨园，那段时间梨园的调用会在代理日志里
也留一笔。代理行的 `id` 就是 DeepSeek 响应 id，与梨园的 `responseId` 相同 ——
**tavern 行若与某条 liyuan 行同 id，跳过**（梨园那条信息更全）。实测迁移来的 6 行
没有命中，但规则保留，以防有人把代理再接回梨园。

## 不存金额

只存 token 与 UTC 时刻；金额在读取时由 `llm_pricing` 按每条调用的时刻与所选模型算。
理由见 `llm_pricing` 模块 docstring。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

from app.core.logging import get_logger
from app.features.llm_usage.liyuan_billing import is_peak

logger = get_logger("LLM_USAGE")

DSH_LEDGER_DIR = Path(os.environ.get("DSH_USAGE_LEDGER_DIR", "/root/.dsh/usage-ledger"))
TAVERN_LEDGER_DIR = Path(os.environ.get("TAVERN_USAGE_LEDGER_DIR", "/root/st-proxy/ledger"))

LEDGER_SOURCES: dict[str, Path] = {"dsh": DSH_LEDGER_DIR, "tavern": TAVERN_LEDGER_DIR}


# ── 纯函数：行 → 文档 ──


def _int_or_none(value) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def ledger_row_to_doc(row: Mapping, default_source: str) -> Optional[dict]:
    """把一行 v1 账本映射成 Mongo 文档；格式不认识返回 None（跳过，不抛）。"""
    if not isinstance(row, Mapping) or row.get("v") != 1:
        return None
    rid = row.get("id")
    ts_ms = row.get("ts")
    if not isinstance(rid, str) or not rid or not isinstance(ts_ms, (int, float)):
        return None
    ts = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
    return {
        "id": rid,
        "source": row.get("source") or default_source,
        "ts": ts,
        "provider": row.get("provider"),
        "model": row.get("model"),
        "session_id": row.get("sessionId"),
        "purpose": row.get("purpose"),
        "finish": row.get("finish"),
        "miss": _int_or_none(row.get("miss")),
        "hit": _int_or_none(row.get("hit")),
        "cache_write": _int_or_none(row.get("cacheWrite")),
        "output": _int_or_none(row.get("output")),
        "reasoning": _int_or_none(row.get("reasoning")),
        "backfill": row.get("src") == "backfill",
        "peak": is_peak(ts),
    }


def split_complete_lines(chunk: bytes) -> tuple[list[bytes], int]:
    """切出完整行，返回 (行列表, 消费掉的字节数)。

    结尾没有 `\\n` 的半行不算、不消费 —— 写入方可能还没写完（见模块 docstring）。
    """
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], 0
    return [ln for ln in chunk[:end].split(b"\n") if ln.strip()], end + 1


def liyuan_record_to_doc(rec) -> dict:
    """把 `liyuan_usage.UsageRecord` 映射成统一文档。"""
    rid = rec.response_id or f"liyuan:{rec.session_id}:{rec.seq}"
    return {
        "id": rid,
        "source": "liyuan",
        "ts": rec.ts,
        "provider": "deepseek",
        "model": rec.model,
        "session_id": rec.session_id,
        "purpose": None,
        "finish": None,
        "miss": rec.miss,
        "hit": rec.hit,
        "cache_write": rec.cache_write,
        "output": rec.output,
        "reasoning": rec.reasoning,
        "backfill": False,
        "peak": is_peak(rec.ts),
    }


def drop_cross_source_duplicates(docs: Iterable[dict], liyuan_ids: set[str]) -> list[dict]:
    """去掉与梨园同 id 的 tavern 行（见模块 docstring「跨来源去重」）。"""
    return [d for d in docs if not (d["source"] == "tavern" and d["id"] in liyuan_ids)]


# ── IO ──


def iter_ledger_files(base: Path) -> Iterator[Path]:
    if not base.exists():
        return
    yield from sorted(p for p in base.glob("*.jsonl") if p.is_file())


@dataclass
class IngestResult:
    files: int = 0
    lines: int = 0
    upserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"files": self.files, "lines": self.lines, "upserted": self.upserted,
                "skipped": self.skipped, "errors": self.errors}


def _ingest_ledger_file(path: Path, source: str, repo, liyuan_ids: set[str], result: IngestResult) -> None:
    key = str(path)
    size = path.stat().st_size
    offset = repo.get_offset(key)
    if size < offset:  # 文件被替换（如重跑回填）：从头读，id 唯一索引兜住重复
        offset = 0
    if size == offset:
        return
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    lines, consumed = split_complete_lines(chunk)
    docs = []
    for ln in lines:
        result.lines += 1
        try:
            doc = ledger_row_to_doc(json.loads(ln), source)
        except (ValueError, TypeError):
            doc = None
        if doc is None:
            result.skipped += 1
            continue
        docs.append(doc)
    kept = drop_cross_source_duplicates(docs, liyuan_ids)
    result.skipped += len(docs) - len(kept)
    if kept:
        result.upserted += repo.upsert_many(kept)
    repo.set_offset(key, offset + consumed)


def ingest(ledger_sources: Optional[dict[str, Path]] = None, liyuan_root: Optional[Path] = None) -> IngestResult:
    """三个来源各扫一遍，幂等写入。先梨园（建立去重用的 id 集合），再账本。"""
    from app.features.llm_usage.repo import llm_usage_repo
    from app.features.llm_usage.liyuan_usage import iter_session_files, parse_session_file

    result = IngestResult()

    liyuan_docs: list[dict] = []
    for path in iter_session_files(liyuan_root):
        result.files += 1
        try:
            liyuan_docs.extend(liyuan_record_to_doc(r) for r in parse_session_file(path))
        except Exception as exc:  # noqa: BLE001 — 单文件失败不中断
            logger.exception("梨园会话解析失败 %s", path)
            result.errors.append(f"{path.name}: {exc}")
    if liyuan_docs:
        result.upserted += llm_usage_repo.upsert_many(liyuan_docs)
    liyuan_ids = {d["id"] for d in liyuan_docs}

    for source, base in (ledger_sources or LEDGER_SOURCES).items():
        for path in iter_ledger_files(base):
            result.files += 1
            try:
                _ingest_ledger_file(path, source, llm_usage_repo, liyuan_ids, result)
            except Exception as exc:  # noqa: BLE001
                logger.exception("账本导入失败 %s", path)
                result.errors.append(f"{path.name}: {exc}")

    try:
        result_sessions = resolve_session_cwds(llm_usage_repo)
        if result_sessions:
            logger.info("新解析 %d 个 DSH 会话的工作目录", result_sessions)
    except Exception as exc:  # noqa: BLE001 — 只影响按项目归并，不影响花费
        logger.exception("解析 DSH 会话工作目录失败")
        result.errors.append(f"sessions: {exc}")

    logger.info("LLM 用量导入：%d 文件 / %d 行 / 写入 %d / 跳过 %d",
                result.files, result.lines, result.upserted, result.skipped)
    return result


DSH_SESSIONS_ROOT = Path(os.environ.get("DSH_SESSIONS_ROOT", "/root/.dsh/sessions"))
_SESSION_LOG = re.compile(r"^session(?:\.v(\d+))?\.jsonl(\.zstd)?$")


def read_session_header(path: Path) -> Optional[dict]:
    """读 DSH 会话日志的首行（session header）。zstd 用系统 `zstd -dc` 流式解，
    只取第一行就关管道 —— 大会话日志有几 MB，没必要整份解开。venv 里没有 zstd 模块。"""
    import subprocess

    try:
        if path.suffix == ".zstd":
            proc = subprocess.Popen(["zstd", "-dcq", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                line = proc.stdout.readline()
            finally:
                proc.kill()
                proc.wait()
        else:
            with path.open("rb") as fh:
                line = fh.readline()
        head = json.loads(line)
    except (OSError, ValueError):
        return None
    return head if isinstance(head, dict) and head.get("type") == "session" else None


def resolve_session_cwds(repo, root: Optional[Path] = None) -> int:
    """把还没缓存过的 DSH 会话的 sessionId → cwd 写进 `llm_usage_sessions`。

    会话目录名就是 sessionId（实测 576/576），先比对已知集合，只开新会话的日志，
    所以每轮增量几乎为零。cwd 在会话创建时写死，缓存永不失效。
    """
    base = root or DSH_SESSIONS_ROOT
    if not base.exists():
        return 0
    known = repo.known_session_ids()
    new = []
    for ws in base.iterdir():
        if not ws.is_dir():
            continue
        for sd in ws.iterdir():
            if sd.name in known or not sd.is_dir():
                continue
            logs = [p for p in sd.iterdir() if _SESSION_LOG.match(p.name)]
            if not logs:
                continue
            newest = max(logs, key=lambda p: int(_SESSION_LOG.match(p.name).group(1) or 1))
            head = read_session_header(newest)
            if head and head.get("id"):
                new.append({"_id": head["id"], "cwd": head.get("cwd"),
                            "parent": head.get("parentSession"), "depth": head.get("delegationDepth")})
    repo.upsert_sessions(new)
    return len(new)


def run_llm_usage_ingest() -> dict:
    """调度器入口 —— 永不抛异常：用量统计是旁路功能。"""
    try:
        return ingest().as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM 用量导入失败")
        return {"files": 0, "lines": 0, "upserted": 0, "skipped": 0, "errors": [str(exc)]}
