"""Joybuy 降价监控 API。

cookie 更新走 **POST body**，不走 query string。理由与 Mercor 凭证那条相同：
uvicorn 的 access log 只记 method+path、不记 body，浏览器历史也只记 URL ——
所以「凭证会留在日志里」这个担心**只对 query string 成立**。
⚠️ 绝不能改成 GET 或查询参数。响应一律不回显 cookie 原文。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import require_admin_token
from app.core.settings import settings
from app.features.joybuy import repo as state
from app.features.joybuy.spider import check_price_sync

router = APIRouter(prefix="/api/joybuy", tags=["Joybuy 降价监控"])


class CookiePayload(BaseModel):
    cookie: str = Field(..., min_length=10, description="至少要含 mfs_session=...")


class ThresholdPayload(BaseModel):
    threshold: float = Field(..., gt=0, description="现价低于该值(欧元)时推送")


@router.get("/status", summary="监控状态（不含 cookie 原文）")
def get_status():
    snap = state.snapshot()
    snap.update(
        {
            "sku": settings.joybuy_sku,
            "url": settings.joybuy_product_url,
            "topic": settings.joybuy_ntfy_topic,
            "interval_minutes": settings.joybuy_scan_interval_minutes,
        }
    )
    return snap


@router.get("/history", summary="价格变化历史（只在价格变动时留痕）")
def get_history(limit: int = 200, sku: Optional[str] = None):
    return state.history(sku or settings.joybuy_sku, limit)


@router.post(
    "/cookie",
    summary="更新登录 cookie（走 body，绝不走 query）",
    dependencies=[Depends(require_admin_token)],
)
def update_cookie(payload: CookiePayload):
    cookie = payload.cookie.strip()
    if "mfs_session=" not in cookie:
        # 早失败好过「存进去了但每小时静默失败一次」。mfs_session 是实测
        # 唯一必需的那个，缺了它整份 cookie 都没用。
        raise HTTPException(400, "cookie 里必须含 mfs_session=，否则一定会被风控拦回")
    state.set_cookie(cookie)
    # 存完立刻跑一轮验证 —— 用户最需要确认「这份新 cookie 行不行」的时刻
    # 就是刚贴进去那一秒，让他等一个小时才知道结果是很差的体验。
    result = check_price_sync()
    return {"saved": True, "cookie_length": len(cookie), "verify": result}


@router.post(
    "/threshold",
    summary="修改推送阈值（免重启）",
    dependencies=[Depends(require_admin_token)],
)
def update_threshold(payload: ThresholdPayload):
    state.set_threshold(payload.threshold)
    # 阈值调高时旧的推送节流可能挡住新的提醒，一并清掉
    state.reset_notify(settings.joybuy_sku)
    return {"threshold": payload.threshold}


@router.post(
    "/check",
    summary="立刻跑一轮",
    dependencies=[Depends(require_admin_token)],
)
def run_check():
    return check_price_sync()


@router.post(
    "/reset-notify",
    summary="清掉推送节流（想重新收到同价位提醒时用）",
    dependencies=[Depends(require_admin_token)],
)
def reset_notify():
    state.reset_notify(settings.joybuy_sku)
    return {"reset": True}
