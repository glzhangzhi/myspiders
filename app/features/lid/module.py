"""入籍考试（Leben in Deutschland / Einbürgerungstest）刷题。约定与踩坑见 docs/lid.md。"""

from app.core.module import Module, NavItem
from app.features.lid.router import router

MODULE = Module(
    id="lid", title="入籍考试",
    routers=[router],
    nav=[NavItem("lid", "入籍考试", "📝", group="life", order=35)],
)
