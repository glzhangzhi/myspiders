"""Nintendo eShop 折扣 API 路由。"""

from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from app.features.nintendo.models import NinPriceRecord
from app.features.nintendo.repo import NinPriceRepository

router = APIRouter(prefix="/api/nintendo", tags=["Nintendo eShop"])
_repo = NinPriceRepository()

# 折扣面向(身处德国的)使用者，"今日"按德国日历判断
TZ_DE = ZoneInfo("Europe/Berlin")

# 简体→繁体转换 (lazy init)
_s2t_converter = None

def _to_traditional(text: str) -> str:
    global _s2t_converter
    if _s2t_converter is None:
        from opencc import OpenCC
        _s2t_converter = OpenCC('s2t')
    return _s2t_converter.convert(text)


@router.get("/prices", response_model=List[NinPriceRecord], summary="所有追踪的游戏价格")
def list_all_prices():
    records = _repo.fetch_all()
    return list(records.values())


@router.get("/discounts", summary="当前折扣列表（按折扣力度排序）")
def list_discounts(
    min_discount: float = 0,
    max_price: Optional[float] = Query(None, description="最高现价筛选 (≤)"),
):
    """列出当前有折扣的游戏，可按最低折扣力度和最高价格过滤。"""
    records = _repo.fetch_all()
    discounted = [
        r for r in records.values()
        if r.discount_percent >= min_discount and r.current_price < r.original_price
    ]
    if max_price is not None:
        discounted = [r for r in discounted if r.current_price <= max_price]
    discounted.sort(key=lambda r: r.discount_percent, reverse=True)
    return discounted


@router.get("/discounts/new", summary="今日新增折扣")
def list_new_discounts():
    """列出今天更新的折扣游戏。"""
    records = _repo.fetch_all()
    today = datetime.now(TZ_DE).date()
    new_discounts = [
        r for r in records.values()
        if r.current_price < r.original_price
        and r.updated_at.astimezone(TZ_DE).date() == today
    ]
    new_discounts.sort(key=lambda r: r.discount_percent, reverse=True)
    return new_discounts


@router.get("/prices/{pid}", response_model=NinPriceRecord, summary="查询单个游戏价格")
def get_price(pid: str):
    records = _repo.fetch_all()
    record = records.get(pid)
    if not record:
        raise HTTPException(status_code=404, detail=f"未找到 pid={pid} 的记录")
    return record


@router.get("/search", summary="按游戏名搜索")
def search_by_name(q: str = Query(..., description="游戏名（支持简体中文，自动转繁体）")):
    """搜索游戏名，同时匹配简体和繁体。"""
    q_lower = q.lower()
    q_trad = _to_traditional(q).lower()
    records = _repo.fetch_all()
    results = [
        r for r in records.values()
        if q_lower in r.title.lower() or q_trad in r.title.lower()
    ]
    results.sort(key=lambda r: r.discount_percent, reverse=True)
    return results
