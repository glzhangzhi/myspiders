"""菜谱库 + 推送列表 + 推 Kindle。"""

from app.core.module import LegacyPage, Module, NavItem
from app.core.paths import DASHBOARD_DIR
from app.features.recipe.router import router

MODULE = Module(
    id="recipe", title="菜谱",
    routers=[router],
    nav=[NavItem("recipe", "菜谱", "🍳", group="life", order=30, legacy_url="/legacy/recipe")],
    legacy_pages=[LegacyPage("/legacy/recipe", DASHBOARD_DIR / "recipe.html", "菜谱页面")],
)
