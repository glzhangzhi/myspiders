"""奇门遁甲排盘。"""

from app.core.module import Module, NavItem
from app.features.qimen.router import router

MODULE = Module(
    id="qimen", title="奇门遁甲",
    routers=[router],
    nav=[NavItem("qimen", "奇门遁甲", "🔮", group="life", order=50)],
)
