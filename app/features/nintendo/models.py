"""任天堂折扣记录模型。"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NinPriceRecord(BaseModel):
    """一个游戏的折扣状态。

    数据源只有「特价」列表页，所以「在列表里」= 在打折，「上次在、这次不在」= 折扣结束。
    记录永不删除：结束的折扣留着，下次再出现时才认得出是「重新打折」而不是新游戏。
    """

    pid: str
    title: str
    original_price: float
    current_price: float          # 打折中 = 折后价；已结束 = 结束前最后一次的折后价（不再有效）
    discount_percent: float
    # 折扣信息最近一次变化：开始打折、重新打折、变价。页面「按更新时间」排的就是它。
    # 折扣结束不改它（否则刚结束的会排到最前面，混进新折扣里）。
    updated_at: datetime = Field(default_factory=_now)
    # 旧记录没有这个字段，按「在打折」读进来；下一次完整抓取会把不在列表里的纠正成已结束。
    on_sale: bool = True
    sale_started_at: Optional[datetime] = None   # 这一轮折扣开始的时间
    sale_ended_at: Optional[datetime] = None     # 最近一次折扣结束的时间（on_sale=False 时有意义）
    last_seen_at: Optional[datetime] = None      # 最近一次在特价列表里看到
