"""Claude 账户用量 API — 供仪表盘「Claude 用量」标签。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.features.claude_usage.service import claude_usage_manager

router = APIRouter(prefix="/api/claude-usage", tags=["Claude 用量"])


# ── Token 验证（支持 Bearer 和 x-token） ──


def _verify_token(request: Request):
    """验证请求 token，支持 Bearer 和 x-token 两种方式。"""
    from app.core.settings import settings

    if not settings.admin_token:
        return
    # 优先检查 Authorization: Bearer <token>
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        if auth.removeprefix("Bearer ").strip() == settings.admin_token:
            return
    # 兼容 x-token header
    x_token = request.headers.get("x-token", "")
    if x_token == settings.admin_token:
        return
    raise HTTPException(status_code=403, detail="Invalid token")


@router.get("", summary="获取 Claude 账户用量（缓存快照）")
async def get_usage(request: Request):
    _verify_token(request)
    return claude_usage_manager.get()


@router.post("/refresh", summary="立即刷新 Claude 账户用量")
async def refresh_usage(request: Request):
    _verify_token(request)
    return await claude_usage_manager.poll()


@router.get("/history", summary="Claude 用量消耗聚合（图表/热力图）")
async def get_usage_history(
    request: Request,
    view: str = "hour",
    window: str = "5h",
    days: int = 30,
):
    _verify_token(request)
    days = max(1, min(days, 90))
    return claude_usage_manager.history_aggregate(view=view, window=window, days=days)


@router.get("/predict", summary="预测窗口结束时的 Claude 利用率")
async def get_usage_predict(request: Request):
    _verify_token(request)
    return claude_usage_manager.predict()
