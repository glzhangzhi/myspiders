"""功能模块自动发现。

扫描 `app.features.*.module`，收集每个包导出的 `MODULE`。

单个模块导入失败**不拖垮整个服务**：记日志、跳过，并在 `/api/modules` 里带上
`error` 让面板看得见（否则少一个入口很难察觉）。测试环境设
`MYSPIDERS_STRICT_MODULES=1`，导入失败直接抛出。
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from dataclasses import dataclass
from typing import Optional

from app.core.logging import get_logger
from app.core.module import Module

logger = get_logger("REGISTRY")


@dataclass
class Loaded:
    id: str
    module: Optional[Module]
    error: Optional[str] = None


def discover() -> list[Loaded]:
    import app.features as pkg

    strict = os.environ.get("MYSPIDERS_STRICT_MODULES") == "1"
    out: list[Loaded] = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        name = f"app.features.{info.name}.module"
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:          # 这个包没有 module.py：不是功能模块
                continue
            if strict:
                raise
            logger.exception("功能模块 %s 导入失败，已跳过", info.name)
            out.append(Loaded(info.name, None, f"{type(exc).__name__}: {exc}"))
            continue
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            logger.exception("功能模块 %s 导入失败，已跳过", info.name)
            out.append(Loaded(info.name, None, f"{type(exc).__name__}: {exc}"))
            continue
        m = getattr(mod, "MODULE", None)
        if not isinstance(m, Module):
            msg = f"{name} 没有导出 MODULE"
            if strict:
                raise RuntimeError(msg)
            out.append(Loaded(info.name, None, msg))
            continue
        out.append(Loaded(m.id, m))
    return out


_LOADED: Optional[list[Loaded]] = None


def loaded() -> list[Loaded]:
    """进程内只发现一次（main 与 /api/modules 共用同一份结果）。"""
    global _LOADED
    if _LOADED is None:
        _LOADED = discover()
    return _LOADED


def modules() -> list[Module]:
    return [x.module for x in loaded() if x.module is not None]
