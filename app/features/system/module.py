"""系统：爬虫管理（调度 + SSE）、手动推送、磁盘/Mongo/备份健康、每日备份。"""

from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.core.module import Job, Module, NavItem
from app.core.settings import settings
from app.features.system.backup import run_backup
from app.features.system.health import _disk, _drive
from app.features.system.push_router import router as push_router
from app.features.system.router import router as system_router
from app.features.system.spiders_router import router as spiders_router


def _jobs():
    return [Job(
        spider_id="backup",
        name="备份到 Google Drive",
        func=run_backup,
        trigger=CronTrigger(hour=4, minute=0, timezone=ZoneInfo(settings.default_timezone)),
        description="每天 04:00 mongodump 全库 + 模块目录 rclone 同步 + 工作区快照到 Google Drive",
        category="system",
        schedule="每天 04:00",
    )]


def _startup():
    # 预热：打开「系统」页时不必现场等 du / rclone
    _disk.cache.prime()
    _drive.cache.prime()


MODULE = Module(
    id="system", title="系统",
    routers=[spiders_router, push_router, system_router], jobs=_jobs, on_startup=_startup,
    nav=[NavItem("health", "系统", "💾", group="ops", order=90)],
)
