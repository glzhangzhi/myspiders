"""备份服务 — mongodump 全库 + 各模块声明的目录 + 工作区文件,经 rclone 同步到 Google Drive。

依赖(运行机器需具备):
  - mongodump（mongodb-database-tools）、tar
  - rclone，且已配置名为 "google drive" 的 remote
备份目标:  google drive:myspiders-backup/{db,files,<模块目录>}/
  - db/     mongo-YYYY-MM-DD.archive.gz   保留 3 份
  - files/  files-YYYY-MM-DD.tar.gz       保留 2 份(每份约 1GB)（酒馆/梨园/lorebook/dsh/memory/.env/vaultwarden/ntfy）
  - <name>/                               各模块 `Module.backup_dirs` 声明的目录，sync 镜像
                                          （只增不改的大量小文件，如图片库）
⚠️ files 里有 .env 和 bot.session（等同账号密码），Drive 账号本身就是最后一道门。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.clients.ntfy import get_ntfy_client
from app.core.logging import get_logger
from app.core.settings import settings

logger = get_logger("BACKUP")

# 复用已订阅的通用 topic:新开 topic 手机上没订阅 = 告警照样看不见
BACKUP_NTFY_TOPIC = "spider"

RCLONE_REMOTE = "google drive:myspiders-backup"
from app.core.paths import DATA_DIR, REPO_ROOT  # noqa: E402
STATUS_FILE = DATA_DIR / "backup_status.json"
DB_KEEP = 3  # 保留最近 N 份数据库快照（Drive 只有 15G，见 FILES_KEEP 处的说明）

# 轮转删旧快照时绕过回收站。Drive 默认把删除的文件放进回收站 30 天、照样占配额：
# 每天删一份 ~1.2G 的工作区包 = 每天往回收站塞 1.2G。2026-11 回收站涨到 1G+、
# Drive 只剩 1.2G，下一次备份就会撑爆（撑爆后 DB 快照也写不进去）。
_DELETE_FLAGS = ["--drive-use-trash=false"]

# rclone 并发。
# 历史:rclone.conf 曾未配 client_id → 走 rclone 内置公共 Google 项目,配额全球共享,
# av_images(3 万+ 小文件、2000+ 子目录)必撞 403 Quota exceeded,连挂两天。
# 现已改用自建 Google Cloud 项目(myspiders-backup)的 OAuth 凭据,配额独享,
# 故把当时为绕配额而设的限流放宽回高并发。
#
# --fast-list 保留:递归一次列完整棵树,把上千次 list 压成几十次,是纯收益。
# 重试保留:偶发 5xx/网络抖动仍应由 rclone 自己退避重试,而不是整单失败。
# 若哪天又出 403,先跑 scripts/verify_rclone_quota.sh 确认凭据是否仍是自建项目。
RCLONE_FLAGS = [
    "--transfers", "16",
    "--checkers", "16",
    "--fast-list",
    "--drive-pacer-min-sleep", "10ms",
    "--drive-pacer-burst", "200",
    "--low-level-retries", "20",
    "--retries", "5",
    "--retries-sleep", "30s",
]

def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _backup_db() -> dict:
    """mongodump 全库(archive+gzip) → rclone 上传,并保留最近 DB_KEEP 份。"""
    name = f"mongo-{datetime.now().strftime('%Y-%m-%d')}.archive.gz"
    with tempfile.NamedTemporaryFile(suffix=".archive.gz", delete=False) as tmp:
        archive = tmp.name
    try:
        r = _run(["mongodump", "--uri", settings.mongo_uri, f"--archive={archive}", "--gzip"], 600)
        if r.returncode != 0:
            raise RuntimeError(f"mongodump 失败: {r.stderr.strip()[:300]}")
        size = Path(archive).stat().st_size
        r = _run(["rclone", "copyto", archive, f"{RCLONE_REMOTE}/db/{name}"], 600)
        if r.returncode != 0:
            raise RuntimeError(f"rclone 上传 DB 失败: {r.stderr.strip()[:300]}")
        # 清理旧快照(按文件名/日期排序,保留最近 DB_KEEP 份)
        ls = _run(["rclone", "lsf", f"{RCLONE_REMOTE}/db/"], 120)
        snaps = sorted(f for f in ls.stdout.splitlines() if f.startswith("mongo-"))
        for old in snaps[:-DB_KEEP]:
            _run(["rclone", "deletefile", f"{RCLONE_REMOTE}/db/{old}", *_DELETE_FLAGS], 120)
        return {"ok": True, "file": name, "size": size}
    finally:
        Path(archive).unlink(missing_ok=True)


def mirror_dirs() -> dict[str, Path]:
    """各模块声明要 sync 镜像的目录 {远端子目录名: 本地路径}。"""
    from app.core.registry import modules

    out: dict[str, Path] = {}
    for m in modules():
        out.update(m.backup_dirs)
    return out


def _backup_mirror(name: str, local: Path) -> dict:
    """rclone sync 一个目录(增量,仅传变化)。"""
    if not local.exists():
        return {"ok": True, "skipped": "目录不存在"}
    r = _run(["rclone", "sync", str(local), f"{RCLONE_REMOTE}/{name}", *RCLONE_FLAGS], 7200)
    if r.returncode != 0:
        raise RuntimeError(f"rclone sync {name} 失败: {r.stderr.strip()[:300]}")
    info: dict = {}
    sz = _run(["rclone", "size", f"{RCLONE_REMOTE}/{name}", "--json"], 300)
    try:
        info = json.loads(sz.stdout)
    except Exception:
        pass
    return {"ok": True, "count": info.get("count"), "size": info.get("bytes")}


# ─────────────────── 工作区文件（2026-10-31 新增）───────────────────
#
# 原先只备份 Mongo 和 AV 图片：酒馆 / 梨园 / lorebook / dsh 会话 / memory 那时在机主
# 自己的台式机上，丢了好歹还有那块硬盘。搬到一台免费云主机后，这些东西只剩一份，
# Oracle 停号或盘坏一次就全没了 —— 所以搬家之前先把备份范围补齐。
#
# 为什么打 tar 快照而不是 rclone sync：sync 是镜像，本地误删一个角色卡，下次备份
# 会把 Drive 上那份也删掉，备份就失去了「回到昨天」的能力。按天打包、保留
# FILES_KEEP 份，跟 DB 快照用同一种语义。图片仍走 sync，因为它 3 万多个小文件、
# 只增不改，打包每天都要重传几个 G。
#
# 只排除**可重建**的东西（依赖、缓存、模型）。宁可多备份几十 MB，也不要漏掉一个
# 不能重建的文件。不存在的路径直接跳过：同一份代码在 WSL 和新机器上都要能跑，
# 两边的目录不一定一模一样。

# 一份约 1.07GB（2026-10-31 实测），Drive 15G 里只剩 8.5G。14 份会撑爆 Drive，
# 而且撑爆后连 DB 快照也写不进去。2026-11-07 每份涨到 ~1.2G、Drive 余量见底，
# 机主拍板降到 2 份（今天 + 昨天），DB 同时降到 3 份。
FILES_KEEP = 2
FILE_SOURCES: list[tuple[str, str]] = [
    # (标签, 路径)。标签就是打包后的顶层目录名，恢复时一看就知道是什么
    ("sillytavern-data", "/root/sillytavern/data"),
    ("sillytavern-config", "/root/sillytavern/config.yaml"),
    ("st-proxy", "/root/st-proxy"),
    ("liyuan", "/root/liyuan"),
    ("liyuan-agent", "/root/.liyuan"),
    ("local-lorebook", "/root/local_lorebook"),
    ("dsh", "/root/.dsh"),
    ("memory", "/root/memory"),
    ("basic-memory", "/root/basic-memory"),
    ("basic-memory-state", "/root/.basic-memory"),
    ("myspiders-env", str(REPO_ROOT / ".env")),
    ("myspiders-sessions", str(REPO_ROOT / "bot.session")),
    # Vaultwarden（2026-11-04 新增）。⚠️ 不能直接打包 data/db.sqlite3：它是 WAL 模式、
    # 运行中在写，tar 出来可能是坏库。宿主机上的 vaultwarden-snapshot.timer 每小时用
    # sqlite3 .backup 生成一份一致快照到 backup/，这里打包的是那份快照。
    # rsa_key.pem 丢了所有客户端都要重新登录；attachments / sends 是库外的文件。
    ("vaultwarden-db", "/root/vaultwarden/backup"),
    ("vaultwarden-key", "/root/vaultwarden/data/rsa_key.pem"),
    ("vaultwarden-attachments", "/root/vaultwarden/data/attachments"),
    ("vaultwarden-sends", "/root/vaultwarden/data/sends"),
    ("vaultwarden-compose", "/root/vaultwarden/docker-compose.yml"),
    ("vaultwarden-env", "/root/vaultwarden/.env"),
    # 自建 ntfy（2026-11-07 新增）。只备 auth.db（账号、ACL、令牌）的一致快照和 compose：
    # 原库是 SQLite WAL，直接 tar 可能是坏库，宿主机的 ntfy-snapshot.timer 每小时用
    # sqlite3 .backup 写到 backup/。cache.db 不备 —— 消息只留 72 小时，丢了只是少几条旧通知。
    ("ntfy-auth", "/root/ntfy/backup"),
    ("ntfy-compose", "/root/ntfy/docker-compose.yml"),
]
FILE_EXCLUDES = [
    "node_modules", ".venv", "__pycache__", "*.pyc",
    "_cache", "_webpack",                     # 酒馆前端构建缓存，启动时重建
    "fastembed_cache",                         # basic-memory 的嵌入模型，会重新下载
    "speech-to-text",                          # dsh 的语音模型
    # 酒馆按聊天自动生成的备份（~185MB，内容与 chats/ 重复）。写带标签的全路径：
    # 单写 "backups" 会连带排除 .dsh/workspace-mover/backups。
    "sillytavern-data/default-user/backups",
    # ⚠️ 不能排除整个 .dsh/mcp-servers：里面的 run.sh、token、taiga.sh 没法重建
    # （2026-10-31 恢复到 free-arm 时才发现它们一起被排除了）。真正可重建的
    # 只有它的 node_modules，上面那条规则已经覆盖。
    "*.log", "access.log",
]


def _backup_files() -> dict:
    """工作区文件打成一个 tar.gz → rclone 上传，保留最近 FILES_KEEP 份。"""
    present = [(label, Path(p)) for label, p in FILE_SOURCES if Path(p).exists()]
    if not present:
        return {"ok": True, "skipped": "没有可备份的路径"}
    name = f"files-{datetime.now().strftime('%Y-%m-%d')}.tar.gz"
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        archive = tmp.name
    try:
        # ⚠️ tar 必须在 stage 目录还活着的时候跑：TemporaryDirectory 一出 with 就删，
        # 曾经把 _run 写在 with 外面，tar 每次都报「-C /tmp/tmpXXX: Cannot open」，
        # 上线起（2026-10-31）连败三天、一份工作区备份都没传上去。
        with tempfile.TemporaryDirectory() as stage:
            # 用符号链接把各路径挂到统一的顶层目录名下，tar 用 -h 跟随链接进去，
            # 包里的结构就是 <标签>/...，与原机器的绝对路径解耦，恢复到哪都行。
            for label, path in present:
                (Path(stage) / label).symlink_to(path)
            cmd = ["tar", "-czhf", archive, "-C", stage,
                   *[f"--exclude={e}" for e in FILE_EXCLUDES],
                   # 酒馆 / basic-memory 运行中会改文件。tar 遇到「读的时候变了」会以
                   # 退出码 1 结束，但包是完整可用的，只是那一个文件可能不是最新版。
                   "--warning=no-file-changed", "--warning=no-file-removed",
                   *[label for label, _ in present]]
            r = _run(cmd, 1800)
        if r.returncode not in (0, 1):
            raise RuntimeError(f"tar 失败({r.returncode}): {r.stderr.strip()[:300]}")
        size = Path(archive).stat().st_size
        r = _run(["rclone", "copyto", archive, f"{RCLONE_REMOTE}/files/{name}"], 1800)
        if r.returncode != 0:
            raise RuntimeError(f"rclone 上传工作区失败: {r.stderr.strip()[:300]}")
        ls = _run(["rclone", "lsf", f"{RCLONE_REMOTE}/files/"], 120)
        snaps = sorted(f for f in ls.stdout.splitlines() if f.startswith("files-"))
        for old in snaps[:-FILES_KEEP]:
            _run(["rclone", "deletefile", f"{RCLONE_REMOTE}/files/{old}", *_DELETE_FLAGS], 120)
        return {"ok": True, "file": name, "size": size,
                "sources": [label for label, _ in present]}
    finally:
        Path(archive).unlink(missing_ok=True)


def _notify_failure(result: dict) -> None:
    """备份失败时推 ntfy。

    之前失败只写日志 + 状态文件,调度器 catch 住异常就没了 —— 图片备份连挂两天无人知晓。
    """
    try:
        errs = "\n".join(f"• {e}" for e in result.get("errors", []))
        ok_parts = [k for k in ("db", "files") if isinstance(result.get(k), dict) and result[k].get("ok")]
        ok_parts += [k for k, v in (result.get("mirrors") or {}).items() if v and v.get("ok")]
        blob = errs.lower()
        # 授权失效和配额超限的处置方式完全不同,报警里直接点明,免得又当成一般错误查半天
        if any(s in blob for s in ("unauthorized_client", "invalid_grant",
                                   "couldn't fetch token", "oauth")):
            hint = ("\n\n⚠️ 疑似 Google 授权失效。\n"
                    "修复: rclone config reconnect \"google drive:\"\n"
                    "(若 OAuth 应用退回 Testing 状态,refresh token 会 7 天过期)")
        elif "quota" in blob or "rate" in blob or "403" in blob:
            hint = ("\n\n⚠️ 疑似 Drive API 配额超限。\n"
                    "排查: bash scripts/verify_rclone_quota.sh")
        else:
            hint = ""
        get_ntfy_client().send(
            BACKUP_NTFY_TOPIC,
            "备份失败",
            f"{errs}\n\n成功部分: {', '.join(ok_parts) or '无'}\n"
            f"耗时 {result.get('duration_seconds')}s{hint}",
            priority="high",
            tags=["floppy_disk", "warning"],
        )
    except Exception:  # noqa: BLE001
        logger.exception("备份失败告警推送失败")


def _write_status(status: dict) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        logger.warning("写备份状态失败: %s", e)


def run_backup() -> dict:
    """完整备份:数据库 + 模块目录 + 工作区 → Google Drive。供调度器调用(同步函数)。"""
    start = time.monotonic()
    result: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "db": None,
        "mirrors": {},
        "files": None,
        "ok": True,
        "errors": [],
    }
    try:
        result["db"] = _backup_db()
        logger.info("数据库备份完成: %s", result["db"])
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["errors"].append(f"db: {e}")
        logger.exception("数据库备份失败")
    for name, local in mirror_dirs().items():
        try:
            result["mirrors"][name] = _backup_mirror(name, local)
            logger.info("目录 %s 备份完成: %s", name, result["mirrors"][name])
        except Exception as e:  # noqa: BLE001
            result["ok"] = False
            result["mirrors"][name] = None
            result["errors"].append(f"{name}: {e}")
            logger.exception("目录 %s 备份失败", name)
    try:
        result["files"] = _backup_files()
        logger.info("工作区备份完成: %s", result["files"])
    except Exception as e:  # noqa: BLE001
        result["ok"] = False
        result["errors"].append(f"files: {e}")
        logger.exception("工作区备份失败")
    result["duration_seconds"] = round(time.monotonic() - start, 1)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_status(result)
    from app.features.system.health import refresh_after_backup

    refresh_after_backup()
    if not result["ok"]:
        _notify_failure(result)
        raise RuntimeError("; ".join(result["errors"]))
    return result
