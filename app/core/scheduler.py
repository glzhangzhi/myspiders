"""中央调度服务 — 统一管理所有爬虫的注册、调度、状态跟踪和 SSE 推送。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.logging import get_logger
from app.core.spider_models import SpiderInfo
from app.core.spider_config import spider_config_repo

logger = get_logger("SCHEDULER")


class _SpiderEntry:
    """内部注册条目。"""

    def __init__(
        self,
        spider_id: str,
        name: str,
        func: Callable[..., Any],
        description: str = "",
        category: str = "general",
        schedule: str = "",
        enabled: bool = True,
    ):
        self.spider_id = spider_id
        self.name = name
        self.func = func
        self.description = description
        self.category = category
        self.schedule = schedule
        self.enabled = enabled
        self.last_run_at: Optional[datetime] = None
        self.last_status: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_duration_seconds: Optional[float] = None
        self.job_id: Optional[str] = None

    def to_info(self, scheduler: AsyncIOScheduler) -> SpiderInfo:
        next_run = None
        if self.job_id and self.enabled:
            job = scheduler.get_job(self.job_id)
            if job and job.next_run_time:
                next_run = job.next_run_time
        return SpiderInfo(
            spider_id=self.spider_id,
            name=self.name,
            description=self.description,
            category=self.category,
            enabled=self.enabled,
            schedule=self.schedule,
            last_run_at=self.last_run_at,
            next_run_at=next_run,
            last_status=self.last_status,
            last_error=self.last_error,
            last_duration_seconds=self.last_duration_seconds,
        )


class SpiderScheduler:
    """全局爬虫调度管理器。"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._entries: Dict[str, _SpiderEntry] = {}
        self._subscribers: List[asyncio.Queue] = []
        self._running_tasks: Dict[str, asyncio.Task] = {}

    def register(
        self,
        spider_id: str,
        name: str,
        func: Callable[..., Any],
        trigger=None,
        description: str = "",
        category: str = "general",
        schedule: str = "",
        enabled_default: bool = True,
    ) -> None:
        """注册一个爬虫。在 start() 之前调用。"""

        # 从 MongoDB 读取持久化的 enabled 状态
        db_enabled = spider_config_repo.is_enabled(spider_id)
        enabled = db_enabled if db_enabled is not None else enabled_default

        entry = _SpiderEntry(
            spider_id=spider_id,
            name=name,
            func=func,
            description=description,
            category=category,
            schedule=schedule,
            enabled=enabled,
        )
        self._entries[spider_id] = entry

        if trigger:
            # 用 wrapper 包裹，自动跟踪执行状态
            async def job_wrapper():
                await self._execute_spider(spider_id)

            job = self.scheduler.add_job(
                job_wrapper,
                trigger,
                id=spider_id,
                max_instances=1,
                coalesce=True,
            )
            entry.job_id = job.id

            # 如果禁用，暂停 job
            if not enabled:
                job.pause()

    async def _call_spider(self, func: Callable[..., Any]) -> None:
        """调用爬虫函数:协程函数直接 await,同步函数丢线程池,避免阻塞事件循环。

        调度器运行在 asyncio 事件循环中。若同步爬虫(Playwright 抓取、一串同步
        Mongo/HTTP)直接在循环里调用,会卡住整个服务(SSE、/api/health 全停)。
        因此同步函数交给 asyncio.to_thread 在线程池执行。
        """
        if asyncio.iscoroutinefunction(func):
            await func()
        else:
            result = await asyncio.to_thread(func)
            # 少见情况:同步函数本身返回了协程,再 await 之
            if asyncio.iscoroutine(result):
                await result

    async def _execute_spider(self, spider_id: str) -> None:
        """执行爬虫并跟踪状态。"""

        entry = self._entries.get(spider_id)
        if not entry:
            return

        entry.last_status = "running"
        entry.last_run_at = datetime.now(timezone.utc)
        await self._broadcast(entry)

        start_time = time.monotonic()
        try:
            await self._call_spider(entry.func)
            entry.last_status = "success"
            entry.last_error = None
        except Exception as exc:
            entry.last_status = "failed"
            entry.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("爬虫 %s 执行失败", spider_id)
        finally:
            entry.last_duration_seconds = round(time.monotonic() - start_time, 2)
            await self._broadcast(entry)

    def start(self) -> None:
        """启动调度器。"""
        self.scheduler.start()
        logger.info("调度器已启动，注册了 %d 个爬虫", len(self._entries))

    def shutdown(self) -> None:
        """关闭调度器。"""
        self.scheduler.shutdown(wait=False)

    def list_spiders(self) -> List[SpiderInfo]:
        """返回所有爬虫的状态。"""
        return [entry.to_info(self.scheduler) for entry in self._entries.values()]

    def get_spider(self, spider_id: str) -> Optional[SpiderInfo]:
        """返回单个爬虫状态。"""
        entry = self._entries.get(spider_id)
        if not entry:
            return None
        return entry.to_info(self.scheduler)

    def toggle_spider(self, spider_id: str, enabled: bool) -> Optional[SpiderInfo]:
        """启用或禁用爬虫。"""
        entry = self._entries.get(spider_id)
        if not entry:
            return None

        entry.enabled = enabled
        spider_config_repo.set_enabled(spider_id, enabled)

        if entry.job_id:
            job = self.scheduler.get_job(entry.job_id)
            if job:
                if enabled:
                    job.resume()
                    logger.info("爬虫 %s 已启用", spider_id)
                else:
                    job.pause()
                    logger.info("爬虫 %s 已禁用", spider_id)

        # Fire-and-forget broadcast (safe for both sync and async callers)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast(entry))
        except RuntimeError:
            # No running event loop (called from sync context in worker thread)
            pass
        return entry.to_info(self.scheduler)

    def reschedule_spider(
        self, spider_id: str, trigger, schedule_text: str = ""
    ) -> Optional[SpiderInfo]:
        """替换某个爬虫的触发器（运行时改调度周期，无需重启服务）。

        APScheduler 的 job.reschedule() 会保留 paused 状态，所以禁用中的爬虫改完
        间隔仍然是禁用的 —— 这是期望行为。
        """
        entry = self._entries.get(spider_id)
        if not entry or not entry.job_id:
            return None

        job = self.scheduler.get_job(entry.job_id)
        if not job:
            return None

        was_paused = job.next_run_time is None
        job.reschedule(trigger=trigger)
        # reschedule 会唤醒 paused 的 job，需要手动恢复暂停状态
        if was_paused:
            job.pause()

        if schedule_text:
            entry.schedule = schedule_text
        logger.info("爬虫 %s 调度已更新: %s", spider_id, schedule_text or trigger)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast(entry))
        except RuntimeError:
            pass
        return entry.to_info(self.scheduler)

    async def trigger_spider(self, spider_id: str) -> Optional[SpiderInfo]:
        """手动触发一次爬虫执行。"""
        entry = self._entries.get(spider_id)
        if not entry:
            return None

        # 检查是否已经在运行
        if entry.last_status == "running":
            return entry.to_info(self.scheduler)

        # 在后台执行
        task = asyncio.create_task(
            self._execute_spider(spider_id),
            name=f"manual:{spider_id}",
        )
        self._running_tasks[spider_id] = task

        # 等一小会让状态变成 running
        await asyncio.sleep(0.05)
        return entry.to_info(self.scheduler)

    # ── SSE ──

    def subscribe(self) -> asyncio.Queue:
        """创建一个 SSE 订阅队列。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """移除 SSE 订阅。"""
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _broadcast(self, entry: _SpiderEntry) -> None:
        """向所有订阅者广播状态变更。"""
        info = entry.to_info(self.scheduler)
        data = info.model_dump(mode="json")
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


# 全局单例
spider_scheduler = SpiderScheduler()


def get_scheduler() -> SpiderScheduler:
    """取调度器单例，供**爬虫模块与路由**在函数内延迟调用。

    为什么它们不在模块顶层 `from app.core.scheduler import spider_scheduler`：
    app.main 的 lifespan 会 import 各爬虫模块并逐个 register()，模块顶层 import
    发生在注册之前，拿到的是一个尚未注册完成的调度器。爬虫要用的是运行期已就绪
    的那个实例，所以一律在函数体内取。

    （不是为了避免循环 import —— 本模块不 import 任何爬虫，顶层 import 不会成环，
    只是时机不对。别据此把 import 提回模块顶层。）
    """
    return spider_scheduler
