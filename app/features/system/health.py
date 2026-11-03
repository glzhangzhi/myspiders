"""系统健康统计 — 磁盘 / MongoDB / 备份,供仪表盘「系统」标签。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.clients.mongo import get_mongo_client
from app.core.cache import swr
from app.core.settings import settings
from app.features.system import backup as bk


def _du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        r = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and r.stdout:
            return int(r.stdout.split()[0])
    except Exception:  # noqa: BLE001
        pass
    return 0


def _count(path: Path, suffix: str | None = None, recursive: bool = False) -> int:
    if not path.exists():
        return 0
    items = path.rglob("*") if recursive else path.iterdir()
    n = 0
    for p in items:
        try:
            if p.is_file() and (suffix is None or p.name.endswith(suffix)):
                n += 1
        except OSError:
            pass
    return n


# du 整个目录（图片库 3 万多个文件）要 1 秒以上、rclone about 是网络调用：
# 几分钟内答案几乎不变，过期后台刷新。备份跑完会主动 invalidate。
@swr(600)
def _disk() -> dict:
    du = shutil.disk_usage("/")
    return {
        "system": {"total": du.total, "used": du.used, "free": du.free,
                   "pct": round(du.used / du.total * 100) if du.total else 0},
        # 各模块声明的备份目录（如 AV 图片库）各占一行
        **{name: {"bytes": _du_bytes(path), "count": _count(path, recursive=True)}
           for name, path in bk.mirror_dirs().items()},
    }


def _mongo() -> dict:
    out: dict = {"dbs": {}, "total_bytes": 0}
    try:
        c = get_mongo_client()
        for name in [settings.mongo_default_db]:
            stats = c[name].command("dbstats")
            size = int(stats.get("dataSize", 0))
            out["dbs"][name] = {"bytes": size, "collections": stats.get("collections", 0)}
            out["total_bytes"] += size
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


@swr(600)
def _drive() -> dict | None:
    """rclone about 查 Google Drive 配额(网络调用,带超时)。"""
    remote = bk.RCLONE_REMOTE.split(":")[0] + ":"  # "google drive:"
    try:
        r = subprocess.run(["rclone", "about", remote, "--json"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            return {"total": d.get("total"), "used": d.get("used"), "free": d.get("free")}
    except Exception:  # noqa: BLE001
        pass
    return None


def get_health() -> dict:
    backup: dict = {}
    try:
        if bk.STATUS_FILE.exists():
            backup = json.loads(bk.STATUS_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {
        "disk": _disk(),
        "mongo": _mongo(),
        "backup": backup,
        "drive": _drive(),
        "cached_at": _disk.cache.cached_at,
    }


def refresh_after_backup() -> None:
    _disk.cache.invalidate()
    _drive.cache.invalidate()
