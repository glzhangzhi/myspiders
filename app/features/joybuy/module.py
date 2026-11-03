"""Joybuy 单品降价监控。"""

from apscheduler.triggers.interval import IntervalTrigger

from app.core.module import Job, Module
from app.core.settings import settings
from app.features.joybuy.router import router
from app.features.joybuy.spider import check_price


def _jobs():
    m = settings.joybuy_scan_interval_minutes
    return [Job(
        spider_id="joybuy_price",
        name="Joybuy 降价监控",
        func=check_price,
        trigger=IntervalTrigger(minutes=m),
        description=(
            f"每 {m} 分钟查 SKU {settings.joybuy_sku} 的价格，跌破阈值"
            f"(默认 €{settings.joybuy_price_threshold:.0f})推 ntfy(topic={settings.joybuy_ntfy_topic})"
        ),
        category="monitor",
        schedule=f"每 {m} 分钟",
    )]


# 没有独立页面：价格卡片挂在「系统」页里
MODULE = Module(id="joybuy", title="Joybuy", routers=[router], jobs=_jobs)
