"""梨园（Liyuan）会话 token 用量 — 解析 jsonl。

入库与展示已并入统一 LLM 用量（`app/features/llm_usage/service.py`，2026-11-02），
本模块只保留解析（及解析时附带的计价字段，供测试锁口径）。

## 数据来源

梨园把每一次 LLM 调用的 usage 原样写进会话记录：
`~/.liyuan/agent/sessions/<workspace>/<ISO时间>_<sessionId>.jsonl`

一行一条记录，携带 usage 的有两种：
  - `type=message`  且 `message.role=assistant`
  - `type=custom`   且 `customType=liyuan-process`，usage 在 `data.message.usage`

## 两个必须处理的坑

**① 轮末镜像会造成重复计数。**
梨园在一轮结束时，会把最后一次 `liyuan-process` 的 usage **原样复制**到一条
`type=message` 的 assistant 记录上。不去重则总量虚高约 12%。判据：与上一条
usage 四元组（input/output/cacheRead/cacheWrite）完全相同即视为镜像。

**② jsonl 里的 `cost` 字段一半是假的。**
走本地 st-proxy 的会话 cost 全为 0（代理没配单价），走官方 API 的才有值。
所以**只信 token 数，钱一律用 liyuan_billing 自己算** —— 数据源一半真一半假
是最难查的那种 bug。

## 稳定 id

统一账本以 DeepSeek 的 `responseId` 为主键（缺失时退回 `session_id:seq`）。
jsonl 是**追加写**的，所以同一文件的前 N 条永远不变，seq 可安全用作稳定序号。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from app.core.logging import get_logger
from app.features.llm_usage.liyuan_billing import compute_cost, canonical_model

logger = get_logger("LIYUAN_USAGE")

# 会话根目录。支持用环境变量覆盖，便于测试与将来接入别的机器。
SESSIONS_ROOT = Path(
    os.environ.get("LIYUAN_SESSIONS_ROOT", os.path.expanduser("~/.liyuan/agent/sessions"))
)


@dataclass
class UsageRecord:
    """一次 LLM 调用的完整记录（已计价）。"""

    session_id: str
    seq: int                    # 该会话内的调用序号，从 1 开始
    workspace: str              # 目录名，如 --root-liyuan-- 或 稿件/<id>
    source_file: str
    ts: dt.datetime             # UTC, tz-aware
    model: str

    miss: int                   # 输入 · 未命中
    hit: int                    # 输入 · 命中
    output: int                 # 输出（含 reasoning）
    reasoning: int
    cache_write: int

    turn: int                   # 属于第几轮用户发送（1 开始；0 = 无归属）
    user_excerpt: str           # 该轮用户输入摘要，便于在明细里认出是哪局

    peak: bool
    priced: bool
    cost_miss: float
    cost_hit: float
    cost_output: float
    cost_total: float
    price_model: str = ""
    price_effective_from: Optional[dt.datetime] = None
    # DeepSeek 返回的响应 id。统一用量账本（llm_usage）拿它做主键：
    # 与 st-proxy 记下的 request_id 是同一个值，可据此跨来源去重。
    response_id: str = ""

    def to_doc(self) -> dict:
        """转成 Mongo 文档。金额与峰谷标记一并冻结存储，读取时不再重算。"""
        d = {
            "session_id": self.session_id,
            "seq": self.seq,
            "workspace": self.workspace,
            "source_file": self.source_file,
            "ts": self.ts,
            "model": self.model,
            "miss": self.miss,
            "hit": self.hit,
            "output": self.output,
            "reasoning": self.reasoning,
            "cache_write": self.cache_write,
            "prompt_total": self.miss + self.hit,
            "turn": self.turn,
            "user_excerpt": self.user_excerpt,
            "peak": self.peak,
            "priced": self.priced,
            "cost_miss": self.cost_miss,
            "cost_hit": self.cost_hit,
            "cost_output": self.cost_output,
            "cost_total": self.cost_total,
            "saved_if_offpeak": self.cost_total / 2 if self.peak else 0.0,
            "price_model": self.price_model,
            "price_effective_from": self.price_effective_from,
        }
        return d


def _extract_inner(rec: dict) -> Optional[dict]:
    """从一行记录里取出带 usage 的那层 message，取不到返回 None。"""
    t = rec.get("type")
    if t == "custom":
        data = rec.get("data")
        if isinstance(data, dict):
            msg = data.get("message")
            return msg if isinstance(msg, dict) else None
        return None
    if t == "message":
        msg = rec.get("message")
        return msg if isinstance(msg, dict) else None
    return None


def _content_text(content) -> str:
    """把 content（可能是 str 或 [{type,text}...]）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            x.get("text", "")
            for x in content
            if isinstance(x, dict) and isinstance(x.get("text"), str)
        ]
        return " ".join(parts)
    return ""


def _parse_ts(value) -> Optional[dt.datetime]:
    """解析 ISO 时间戳为 tz-aware UTC。无法解析返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_session_file(path: Path) -> list[UsageRecord]:
    """解析单个会话 jsonl，返回已计价的调用记录（已去重）。

    解析失败的单行会被跳过并记日志 —— 一行坏数据不该让整个文件导不进来。
    """
    records: list[UsageRecord] = []
    session_id = ""
    workspace = path.parent.name
    model = ""
    turn = 0
    user_excerpt = ""
    prev_key: Optional[tuple] = None
    seq = 0

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("读取会话文件失败 %s: %s", path, exc)
        return []

    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("跳过无法解析的行 %s:%d", path.name, lineno)
            continue
        if not isinstance(rec, dict):
            continue

        rtype = rec.get("type")

        if rtype == "session":
            session_id = rec.get("id") or session_id
            continue

        # 模型可能在会话中途切换，记住最近一次
        if rtype == "model_change":
            model = rec.get("modelId") or model
            continue

        inner = _extract_inner(rec)
        if not inner:
            continue

        role = inner.get("role")

        if role == "user":
            turn += 1
            text = _content_text(inner.get("content")).strip().replace("\n", " ")
            user_excerpt = text[:80]
            continue

        usage = inner.get("usage")
        if not isinstance(usage, dict):
            continue

        miss = int(usage.get("input") or 0)
        hit = int(usage.get("cacheRead") or 0)
        out = int(usage.get("output") or 0)
        cw = int(usage.get("cacheWrite") or 0)

        # 轮末镜像去重（见模块 docstring 坑 ①）
        key = (miss, out, hit, cw)
        if key == prev_key:
            prev_key = key
            continue
        prev_key = key

        ts = _parse_ts(rec.get("timestamp"))
        if ts is None:
            logger.warning("跳过缺少时间戳的 usage 记录 %s:%d", path.name, lineno)
            continue

        seq += 1
        used_model = canonical_model(inner.get("modelId") or model)
        cost = compute_cost(
            miss_tokens=miss,
            hit_tokens=hit,
            output_tokens=out,
            ts=ts,
            model=used_model,
        )

        records.append(
            UsageRecord(
                session_id=session_id or path.stem,
                seq=seq,
                workspace=workspace,
                source_file=path.name,
                ts=ts,
                model=used_model,
                miss=miss,
                hit=hit,
                output=out,
                reasoning=int(usage.get("reasoning") or 0),
                cache_write=cw,
                turn=turn,
                user_excerpt=user_excerpt,
                peak=cost.peak,
                priced=cost.priced,
                cost_miss=cost.miss,
                cost_hit=cost.hit,
                cost_output=cost.output,
                cost_total=cost.total,
                price_model=cost.price_model,
                price_effective_from=cost.price_effective_from,
                response_id=str(inner.get("responseId") or ""),
            )
        )

    return records


def iter_session_files(root: Optional[Path] = None) -> Iterator[Path]:
    """遍历所有会话 jsonl。目录不存在时安静返回空。"""
    base = root or SESSIONS_ROOT
    if not base.exists():
        logger.info("梨园会话目录不存在，跳过：%s", base)
        return
    yield from sorted(base.glob("*/*.jsonl"))


def parse_all(root: Optional[Path] = None) -> list[UsageRecord]:
    """解析全部会话。"""
    out: list[UsageRecord] = []
    for path in iter_session_files(root):
        out.extend(parse_session_file(path))
    return out
