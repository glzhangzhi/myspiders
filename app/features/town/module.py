"""AI 小镇：酒馆角色卡 + 机主人设由 DeepSeek 扮演，一步 = 一天。"""

from app.core.module import LegacyPage, Module, NavItem
from app.core.paths import DASHBOARD_DIR
from app.features.town.router import router

MODULE = Module(
    id="town", title="AI 小镇",
    routers=[router],
    nav=[NavItem("town", "AI 小镇", "🏘️", group="life", order=20, legacy_url="/legacy/town")],
    legacy_pages=[LegacyPage("/legacy/town", DASHBOARD_DIR / "town.html", "AI 小镇页面")],
)
