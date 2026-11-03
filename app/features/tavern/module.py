"""酒馆资料：只读扫 SillyTavern 文件，增删改走 tavern_sync。"""

from app.core.module import LegacyPage, Module, NavItem
from app.core.paths import DASHBOARD_DIR
from app.features.tavern.catalog import tavern_catalog
from app.features.tavern.router import router


def _startup():
    # 冷扫描 376 张卡要解 PNG 的 tEXt 块（约 1.6 秒）；启动时后台预热，首次打开页面不用等
    import threading

    threading.Thread(target=tavern_catalog.all_items, name="tavern-prime", daemon=True).start()

MODULE = Module(
    id="tavern", title="酒馆资料",
    routers=[router], on_startup=_startup,
    nav=[NavItem("tavern", "酒馆资料", "🍺", group="collect", order=30, legacy_url="/legacy/tavern")],
    legacy_pages=[LegacyPage("/legacy/tavern", DASHBOARD_DIR / "tavern.html", "酒馆资料页面")],
)
