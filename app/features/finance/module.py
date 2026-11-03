"""账单：N26 对账单 PDF → 逐条标注 → 支出可视化。"""

from app.core.module import LegacyPage, Module, NavItem
from app.core.paths import DASHBOARD_DIR
from app.features.finance.router import router

MODULE = Module(
    id="finance", title="账单",
    routers=[router],
    nav=[NavItem("finance", "账单", "💶", group="work", order=30, legacy_url="/legacy/finance")],
    legacy_pages=[LegacyPage("/legacy/finance", DASHBOARD_DIR / "finance.html", "账单支出页面")],
)
