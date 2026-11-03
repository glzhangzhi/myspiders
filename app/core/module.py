"""功能模块的声明式描述。

每个 `app/features/<id>/module.py` 导出一个 `MODULE = Module(...)`，由
`app.core.registry` 自动发现并挂载：路由、定时任务、启动/关闭钩子、备份路径、
健康探针、导航条目全在这里声明，`app.main` 不再认识任何具体功能。

这样做的直接目的是**公开/私有拆分**：私有功能只是「多几个目录」，公开仓库里
没有它们时核心代码一行不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from fastapi import APIRouter


@dataclass(frozen=True)
class Job:
    """一个定时任务（交给 `SpiderScheduler.register`）。字段与 register 的参数一一对应。"""

    spider_id: str
    name: str
    func: Callable[..., Any]
    trigger: Any = None
    description: str = ""
    category: str = "general"
    schedule: str = ""
    enabled_default: bool = True


@dataclass(frozen=True)
class NavItem:
    """侧边栏里的一个入口。`legacy_url` 非空时前端用 iframe 嵌入旧页面。"""

    id: str
    title: str
    icon: str
    group: str = "life"          # work / life / collect / ops
    order: int = 100
    legacy_url: Optional[str] = None


@dataclass(frozen=True)
class LegacyPage:
    """尚未迁到新前端的整页 HTML，挂在 `url` 下（no-cache）。"""

    url: str
    file: Path
    summary: str = ""


Hook = Callable[[], Optional[Awaitable[None]]]


@dataclass(frozen=True)
class Module:
    id: str
    title: str
    private: bool = False
    routers: Sequence[APIRouter] = ()
    # 用函数而不是列表：任务说明里会读 settings，而 on_load 可能刚改过 settings
    # （如 Mercor 的 world 配置从库里覆盖出厂默认值），必须在 on_load 之后再求值。
    jobs: Optional[Callable[[], Sequence[Job]]] = None
    nav: Sequence[NavItem] = ()
    legacy_pages: Sequence[LegacyPage] = ()
    # 生命周期：on_load 在注册任务之前（同步），on_startup 在调度器启动之后，
    # on_shutdown 在调度器关闭之前。都可以是同步或 async 函数。
    on_load: Optional[Callable[[], None]] = None
    on_startup: Optional[Hook] = None
    on_shutdown: Optional[Hook] = None
    # 备份：需要额外 rclone sync 的目录 {远端子目录名: 本地路径}
    backup_dirs: dict[str, Path] = field(default_factory=dict)
    # 健康页：返回 {名字: {"bytes":…, "count":…}} 之类可 JSON 化的 dict
    health_probe: Optional[Callable[[], dict]] = None
    # FastAPI 异常处理器 {异常类: handler}
    exception_handlers: dict[type, Callable] = field(default_factory=dict)
