"""媒体下载（视频号作者 + YouTube 列表），执行端在另一台 Windows 机器。见 docs/media.md。"""

from apscheduler.triggers.interval import IntervalTrigger

from app.core.module import Job, LegacyPage, Module, NavItem
from app.core.paths import DASHBOARD_DIR
from app.core.settings import settings
from app.features.media.router import router
from app.features.media.service import recover_on_startup, scheduled_scan


def _jobs():
    # 只**检测**不下载（下载永远只在面板点按钮之后发生）。执行端关机是常态 —— 离线静默跳过。
    m = settings.media_scan_interval_minutes
    return [Job(
        spider_id="media_scan",
        name="视频号 / YouTube 更新检测",
        func=scheduled_scan,
        trigger=IntervalTrigger(minutes=m),
        description=(
            f"每 {m} 分钟经 ssh 让执行端检测一次视频号作者与 YouTube 列表，"
            "新条目进待下载列表（自动检测发现新东西才推 low）。\n"
            "那台电脑关着就跳过；视频号还需要电脑微信里打开着视频号页面。"
        ),
        category="media",
        schedule=f"每 {m} 分钟（仅执行端开机时）",
    )]



def _load():
    # 上次进程退出时还在跑的下载：条目退回待下载、运行记为中断
    recover_on_startup()


MODULE = Module(
    id="media", title="下载",
    routers=[router], jobs=_jobs, on_load=_load,
    nav=[NavItem("media", "下载", "📥", group="collect", order=40, legacy_url="/legacy/media")],
    legacy_pages=[LegacyPage("/legacy/media", DASHBOARD_DIR / "media.html", "媒体下载页面")],
)
