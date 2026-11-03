"""计时（timelog）：手机 PWA 计时 + 服务端空闲/超时提醒。见 docs/timelog.md。"""

from apscheduler.triggers.interval import IntervalTrigger

from app.core.module import Job, Module
from app.features.timelog.pwa_router import router as pwa_router
from app.features.timelog.router import router
from app.features.timelog.service import check_reminders


def _jobs():
    # 空闲 / 超时由服务端判断：HyperOS 会杀 PWA 后台，手机自己定不了时。
    # 去重键在 Mongo 里，重启后不会重推；静音时段与「今天不再提醒」在规则里处理。
    return [Job(
        spider_id="timelog_remind",
        name="计时提醒",
        func=check_reminders,
        trigger=IntervalTrigger(minutes=5),
        description=(
            "每 5 分钟检查一次：太久没有计时 → 空闲提醒（low）；"
            "计时远超预估或上限 → 问是不是忘了暂停（high）。\n"
            "晚上和周末静音；每段空闲、每条记录只提醒一次。"
        ),
        category="timelog",
        schedule="每 5 分钟",
    )]


# PWA 挂在 /timelog（独立 scope），不进面板导航
MODULE = Module(id="timelog", title="计时", routers=[router, pwa_router], jobs=_jobs)
