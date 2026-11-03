"""Nintendo eShop（港服）折扣监控。"""

from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.core.module import Job, Module, NavItem
from app.features.nintendo.router import router
from app.features.nintendo.spider import check_price


def _jobs():
    return [Job(
        spider_id="nin_sale",
        name="Nintendo eShop 折扣",
        func=check_price,
        # 按「人在机器前」排期：原 12:00 CN 落在德国 05:00,必然错过。
        trigger=CronTrigger(hour=20, minute=0, timezone=ZoneInfo("Europe/Berlin")),
        description="每天 20:00 (柏林时间) 检查港服 eShop 折扣变化",
        category="nintendo",
        schedule="每天 20:00 (Europe/Berlin)",
    )]


MODULE = Module(
    id="nintendo", title="Nintendo",
    routers=[router], jobs=_jobs,
    nav=[NavItem("nintendo", "Nintendo", "🎮", group="life", order=40)],
)
