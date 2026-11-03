"""DSH 花费按项目 / 大类归并 —— 纯函数 + 一张手写分类表。

## 数据链

账本行带 `session_id`；DSH 每个会话目录 `~/.dsh/sessions/<编码后的 cwd>/<sessionId>/`
里的日志首行（session header）记着精确的 `cwd`。摄取时把 sessionId → cwd 缓存进
Mongo `llm_usage_sessions`（见 `llm_usage.resolve_session_cwds`），这里只做归并。
不从目录名反推 cwd：目录名的编码有损（`-` 与 `/` 分不开，中文被转成 `~XXXX`）。

## 项目 = cwd 的最后一级文件夹名

同一个项目常在好几个路径下出现过（TG视频上传工具 先后在 WSL 远程、`/mnt/c`、`/mnt/e`、
桌面 test 目录），按全路径列会碎成一地，所以**按最后一级文件夹名合并**。
远程工作区（`/root/.dsh/remote-workspaces/<host>-<user>-<port>/...`）的那一段换成
dsh-remote 里保存的机器名，用于显示和按机器分类。代价：不同机器上同名的家目录
（`dev`、`root`）会合成一行 —— 它们本来就落在「未分类」，可以接受。

## 大类：手写表，随时改

`LLM_PROJECT_CATEGORIES` 按顺序匹配，先看项目名、再看机器名；都不中归「未分类」（显示出来，不丢）。
改完重启 myspider 即可，读取时归并，历史数据跟着变。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from app.features.llm_usage.pricing import Bucket, TARGETS

REMOTE_ROOT = "/root/.dsh/remote-workspaces/"
MACHINES_FILE = Path(os.environ.get("DSH_REMOTE_MACHINES", "/root/.dsh/remote-workspaces/machines.json"))

UNCATEGORIZED = "未分类"
UNKNOWN_PROJECT = "（会话已删除）"

# 大类 → 规则。按顺序匹配，第一个命中的生效。
#   projects：项目名（cwd 最后一级文件夹）精确匹配
#   machines：远程机器名（dsh-remote 里保存的 name）匹配，整台机器归一类
# 规则本身是个人数据（项目名、机器名），放在 .env 的 LLM_PROJECT_CATEGORIES（JSON）里，
# 形如 [["工作", {"projects": ["next-step"], "machines": ["work-pc"]}], ...]。
# 每次调用时读取：改完 .env 重启即生效，历史数据跟着重新归并。


def configured_categories() -> list[tuple[str, dict]]:
    from app.core.settings import settings

    raw = (settings.llm_project_categories or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [(str(name), dict(rule)) for name, rule in data]
    except (ValueError, TypeError):
        import logging

        logging.getLogger(__name__).warning("LLM_PROJECT_CATEGORIES 不是合法 JSON，按未分类处理")
        return []


_REMOTE_DIR = re.compile(r"^(?P<host>[^/]+)-(?P<user>[^-/]+)-(?P<port>\d+)$")


def load_machines(path: Path = MACHINES_FILE) -> dict[str, str]:
    """`host-user-port` → 机器名。文件缺失或格式不认识时返回空表（退回显示 IP）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("list", []) if isinstance(data, dict) else data
    out = {}
    for m in items if isinstance(items, list) else []:
        if isinstance(m, dict) and m.get("host") and m.get("name"):
            out[f"{m['host']}-{m.get('username') or 'root'}-{m.get('port') or 22}"] = m["name"]
            # 同一台机器常以别的用户/端口连过（如 3060ti 的 Administrator@22 与 root@2200），
            # 那些组合没单独保存过 —— 按 host 兜底，先保存的名字优先。
            out.setdefault(m["host"], m["name"])
    return out


@dataclass(frozen=True)
class Place:
    """一个 cwd 归并后的位置。"""

    project: str
    machine: Optional[str]  # None = 本机（free-arm）


def place_of(cwd: Optional[str], machines: Mapping[str, str]) -> Place:
    """cwd → (项目, 机器)。"""
    if not cwd:
        return Place(UNKNOWN_PROJECT, None)
    path = cwd.rstrip("/") or "/"
    if path.startswith(REMOTE_ROOT):
        rest = path[len(REMOTE_ROOT):]
        head, _, sub = rest.partition("/")
        m = _REMOTE_DIR.match(head)
        host = m.group("host") if m else head
        machine = machines.get(head) or machines.get(host) or host
        project = sub.rsplit("/", 1)[-1] if sub else f"{machine} 根目录"
        return Place(project, machine)
    return Place(path.rsplit("/", 1)[-1] or "/", None)


def category_of(place: Place, categories=None) -> str:
    categories = configured_categories() if categories is None else categories
    for name, rule in categories:
        if place.project in rule.get("projects", ()):
            return name
    for name, rule in categories:
        if place.machine and place.machine in rule.get("machines", ()):
            return name
    return UNCATEGORIZED


@dataclass
class _Project:
    bucket: Bucket = field(default_factory=Bucket)
    machines: set = field(default_factory=set)


def summarize_projects(
    docs: Iterable[Mapping],
    session_cwd: Mapping[str, str],
    target: str,
    force: bool = False,
    machines: Optional[Mapping[str, str]] = None,
    categories=None,
) -> list[dict]:
    """DSH 调用按 大类 → 项目 汇总，金额口径与 `llm_pricing.summarize` 完全一致。

    只看 `source == "dsh"`：酒馆、梨园不在某个工作目录里用，它们本身就是来源。
    """
    if target not in TARGETS:
        raise ValueError(f"不支持的目标模型：{target}")
    machines = machines or {}
    categories = configured_categories() if categories is None else categories
    cats: dict[str, Bucket] = {}
    projs: dict[tuple[str, str], _Project] = {}
    for doc in docs:
        if doc.get("source") != "dsh":
            continue
        place = place_of(session_cwd.get(doc.get("session_id") or ""), machines)
        cat = category_of(place, categories)
        cats.setdefault(cat, Bucket()).add(doc, target, force)
        p = projs.setdefault((cat, place.project), _Project())
        p.bucket.add(doc, target, force)
        p.machines.add(place.machine or "本机")

    total = sum(b.usd_total for b in cats.values())
    out = []
    for cat, b in sorted(cats.items(), key=lambda kv: -kv[1].usd_total):
        projects = sorted(
            ((name, p) for (c, name), p in projs.items() if c == cat),
            key=lambda kv: -kv[1].bucket.usd_total,
        )
        out.append({
            "category": cat,
            "usd": b.usd_total,
            "share_pct": b.usd_total / total * 100 if total > 0 else 0.0,
            "requests": b.requests,
            "hit_rate": b.hit_rate(),
            "projects": [
                {
                    "project": name,
                    "machines": sorted(p.machines),
                    "usd": p.bucket.usd_total,
                    "share_pct": p.bucket.usd_total / total * 100 if total > 0 else 0.0,
                    "requests": p.bucket.requests,
                    "hit_rate": p.bucket.hit_rate(),
                }
                for name, p in projects
            ],
        })
    return out
