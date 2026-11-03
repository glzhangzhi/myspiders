"""LLM 用量：DSH / 酒馆 / 梨园三个来源 → llm_usage。"""

import asyncio

from apscheduler.triggers.interval import IntervalTrigger

from app.core.module import Job, LegacyPage, Module, NavItem
from app.core.paths import DASHBOARD_DIR
from app.features.llm_usage.router import router
from app.features.llm_usage.service import run_llm_usage_ingest


def _jobs():
    # 纯本地文件扫描 + 幂等 upsert（唯一键 id），账本按字节水位增量读，跑多少次都安全。
    return [Job(
        spider_id="llm_usage",
        name="LLM 用量导入",
        func=run_llm_usage_ingest,
        trigger=IntervalTrigger(minutes=10),
        description="每 10 分钟导入 DSH 账本、酒馆代理账本与梨园会话里的每次模型调用 token",
        category="system",
        schedule="每 10 分钟",
    )]


def _startup():
    # 启动时补一次，页面打开即有数据。ingest 是同步的，丢线程池避免阻塞事件循环。
    asyncio.create_task(asyncio.to_thread(run_llm_usage_ingest))


MODULE = Module(
    id="llm_usage", title="LLM 用量",
    routers=[router], jobs=_jobs, on_startup=_startup,
    nav=[NavItem("usage", "LLM 用量", "💰", group="work", order=40, legacy_url="/legacy/usage")],
    legacy_pages=[LegacyPage("/legacy/usage", DASHBOARD_DIR / "usage.html", "LLM 用量页面")],
)
