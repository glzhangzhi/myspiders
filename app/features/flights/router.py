"""机票查询 API —— 只解析参数、转发给 service。

查询会打外部站点，所以触发类接口（search / day / roundtrip / review）都要 admin token；
自动补全也要（同样是替人请求 Trip）。长查询用 POST + SSE 流式返回：EventSource 带不了
x-token 头，前端用 fetch 读流（web/src/pages/flights/stream.ts）。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.security import require_admin_token
from app.core.settings import settings
from app.features.flights import service
from app.features.flights.client import TripError
from app.features.flights.filters import Filters

router = APIRouter(prefix="/api/flights", tags=["flights"])


class FiltersIn(BaseModel):
    direct_only: bool = False
    max_transfers: Optional[int] = 1
    allow_long_layover: bool = False
    long_layover_hours: float = Field(6.0, gt=0, le=48)
    allow_recheck: bool = False
    recheck_min_layover_hours: float = Field(3.0, ge=0, le=48)
    max_duration_hours: Optional[float] = Field(None, gt=0, le=96)

    def to(self) -> Filters:
        return Filters(**self.model_dump())


class SearchReq(BaseModel):
    mode: str = "ow"
    origin: str
    dest: str
    out_from: date
    out_to: date
    ret_from: Optional[date] = None
    ret_to: Optional[date] = None
    filters: FiltersIn = FiltersIn()


class DayReq(BaseModel):
    origin: str
    dest: str
    date: date
    filters: FiltersIn = FiltersIn()


class RoundtripReq(BaseModel):
    origin: str
    dest: str
    out: date
    ret: date
    filters: FiltersIn = FiltersIn()


class ReviewReq(BaseModel):
    query_id: str


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


@router.get("/config", summary="页面需要的配置（上限、币种、点评是否可用）")
def config():
    return {
        "currency": settings.flights_currency,
        "max_days_oneway": settings.flights_max_days_oneway,
        "max_days_roundtrip": settings.flights_max_days_roundtrip,
        "review_enabled": service.review_enabled(),
        "busy": service.busy(),
        "defaults": Filters().__dict__,
    }


@router.post("/search", summary="逐天查询（SSE：start → day… → done）", dependencies=[Depends(require_admin_token)])
async def search(req: SearchReq):
    try:
        q = service.validate(service.Query(
            mode=req.mode, origin=req.origin, dest=req.dest, out_from=req.out_from, out_to=req.out_to,
            ret_from=req.ret_from, ret_to=req.ret_to, filters=req.filters.to(),
        ))
    except service.QueryError as e:
        raise HTTPException(400, str(e))
    if service.busy():
        raise HTTPException(409, "已有一个查询在跑（全局串行，防止被 Trip 封），等它结束再查")

    async def gen():
        try:
            async for event, data in service.run_query(q):
                yield _sse(event, data)
        except Exception as e:  # 走到这里是 bug；告诉前端而不是静默断流
            yield _sse("fail", {"error": f"{type(e).__name__}: {e}"})
            raise

    return EventSourceResponse(gen(), ping=15)


@router.post("/day", summary="某天的完整明细（含被排除的与原因）", dependencies=[Depends(require_admin_token)])
async def day(req: DayReq):
    try:
        return await service.day_detail(req.origin, req.dest, req.date.isoformat(), req.filters.to())
    except service.QueryError as e:
        raise HTTPException(400, str(e))
    except TripError as e:
        raise HTTPException(502, f"Trip 查询失败：{e}")


@router.post("/roundtrip", summary="真往返价（tripType=2）", dependencies=[Depends(require_admin_token)])
async def roundtrip(req: RoundtripReq):
    try:
        return await service.roundtrip_price(req.origin, req.dest, req.out.isoformat(), req.ret.isoformat(),
                                             req.filters.to())
    except service.QueryError as e:
        raise HTTPException(400, str(e))
    except TripError as e:
        raise HTTPException(502, f"Trip 查询失败：{e}")


@router.post("/review", summary="DeepSeek 点评（查完后调用）", dependencies=[Depends(require_admin_token)])
async def review(req: ReviewReq):
    try:
        return await service.review(req.query_id)
    except service.QueryError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"点评失败：{e}")


@router.get("/places", summary="城市/机场自动补全", dependencies=[Depends(require_admin_token)])
async def places(q: str = ""):
    try:
        return {"items": await service.places(q[:40])}
    except Exception as e:
        raise HTTPException(502, f"自动补全失败：{e}")
