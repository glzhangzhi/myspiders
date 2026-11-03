"""任天堂折扣记录模型。"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class NinPriceRecord(BaseModel):
    """保存每个商品的最新折扣信息。"""

    pid: str
    title: str
    original_price: float
    current_price: float
    discount_percent: float
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
