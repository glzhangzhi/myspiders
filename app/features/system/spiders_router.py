"""Dashboard API 路由 — 爬虫管理 + SSE 实时状态 + 前端页面。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.core.security import require_admin_token
from app.core.spider_models import SpiderToggleRequest

router = APIRouter(tags=["Dashboard"])



def _get_scheduler():
    from app.core.scheduler import spider_scheduler
    return spider_scheduler


# ── 爬虫管理 API ──


@router.get("/api/spiders", summary="列出所有爬虫及状态")
def list_spiders():
    scheduler = _get_scheduler()
    spiders = scheduler.list_spiders()
    return {"spiders": [s.model_dump(mode="json") for s in spiders]}


# SSE 必须在 {spider_id} 之前注册，否则 "events" 会被当作 spider_id
@router.get("/api/spiders/events", summary="SSE 实时状态推送")
async def spider_events(request: Request):
    scheduler = _get_scheduler()
    q = scheduler.subscribe()

    async def event_generator():
        try:
            # 先推送当前所有爬虫状态
            for spider in scheduler.list_spiders():
                yield {
                    "event": "status",
                    "data": json.dumps(spider.model_dump(mode="json"), ensure_ascii=False),
                }

            while True:
                data = await q.get()
                yield {
                    "event": "status",
                    "data": json.dumps(data, ensure_ascii=False),
                }
        except asyncio.CancelledError:
            pass
        finally:
            scheduler.unsubscribe(q)

    return EventSourceResponse(event_generator(), ping=15)


@router.get("/api/spiders/{spider_id}", summary="获取单个爬虫状态")
def get_spider(spider_id: str):
    scheduler = _get_scheduler()
    info = scheduler.get_spider(spider_id)
    if not info:
        raise HTTPException(status_code=404, detail="爬虫不存在")
    return info.model_dump(mode="json")


@router.post(
    "/api/spiders/{spider_id}/toggle",
    summary="启用/禁用爬虫",
    dependencies=[Depends(require_admin_token)],
)
def toggle_spider(spider_id: str, payload: SpiderToggleRequest):
    scheduler = _get_scheduler()
    info = scheduler.toggle_spider(spider_id, payload.enabled)
    if not info:
        raise HTTPException(status_code=404, detail="爬虫不存在")
    return info.model_dump(mode="json")


@router.post(
    "/api/spiders/{spider_id}/trigger",
    summary="手动触发爬虫",
    dependencies=[Depends(require_admin_token)],
)
async def trigger_spider(spider_id: str):
    scheduler = _get_scheduler()
    info = await scheduler.trigger_spider(spider_id)
    if not info:
        raise HTTPException(status_code=404, detail="爬虫不存在")
    return {
        "message": f"已触发 {spider_id}",
        "spider": info.model_dump(mode="json"),
    }


@router.get(
    "/api/verify-token",
    summary="验证管理员令牌",
    dependencies=[Depends(require_admin_token)],
)
def verify_token():
    return {"ok": True}
