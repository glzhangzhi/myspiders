"""爬虫注册与状态模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SpiderInfo(BaseModel):
    """爬虫完整状态信息（运行时计算）。"""

    spider_id: str
    name: str
    description: str = ""
    category: str = "general"
    enabled: bool = True
    schedule: str = ""
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_status: Optional[str] = None  # "success" | "failed" | "running"
    last_error: Optional[str] = None
    last_duration_seconds: Optional[float] = None


class SpiderToggleRequest(BaseModel):
    """启用/禁用爬虫。"""

    enabled: bool
