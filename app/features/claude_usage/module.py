"""Claude 账户用量（自适应轮询：闲时 30min，逼近上限时 5min）。"""

import asyncio

from apscheduler.triggers.interval import IntervalTrigger

from app.core.module import Job, Module, NavItem
from app.features.claude_usage.router import router
from app.features.claude_usage.service import claude_usage_manager


def _jobs():
    return [Job(
        spider_id="claude_usage",
        name="Claude 账户用量",
        func=claude_usage_manager.tick,
        trigger=IntervalTrigger(minutes=5),
        description="自适应拉取 Claude 用量 + 记录历史 + ntfy 阈值/重置预警",
        category="claude",
        schedule="自适应 5–30 分钟",
    )]


def _startup():
    # 启动时立即拉取一次，避免页面初次为空
    asyncio.create_task(claude_usage_manager.poll())


MODULE = Module(
    id="claude_usage", title="Claude 用量",
    routers=[router], jobs=_jobs, on_startup=_startup,
    nav=[NavItem("claude_usage", "Claude 用量", "📊", group="work", order=50)],
)
