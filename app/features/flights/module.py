"""Trip.com 机票查询面板：按日期区间逐天查最低价（单程日历 / 往返矩阵）。没有定时任务。

约定与踩坑见 docs/flights.md。
"""

from app.core.module import Module, NavItem
from app.features.flights.router import router

MODULE = Module(
    id="flights", title="机票",
    routers=[router],
    nav=[NavItem("flights", "机票", "✈️", group="life", order=38)],
)
