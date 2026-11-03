"""进程内的「过期后台刷新」缓存（stale-while-revalidate）。

给那些**慢但不必实时**的探测用：du 整个目录、rclone 查 Drive 配额、ssh 探一台
可能关着的机器。页面打开时它们动辄 1–8 秒，而答案几分钟内几乎不会变。

语义：
  * 第一次调用（没有任何值）：同步计算，调用方等它。
  * 在 ttl 内：直接返回缓存。
  * 超过 ttl：**立刻返回旧值**，同时在后台线程刷新（同一时刻只有一个刷新在跑）。
  * `force=True`：同步重算（面板上「立刻检测」之类的按钮用）。
  * 计算抛异常：首次调用照常抛；后台刷新失败只记日志、保留旧值。

返回值附带 `cached_at`（epoch 秒），前端可据此显示「x 分钟前」。
"""

from __future__ import annotations

import threading
import time
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

from app.core.logging import get_logger

logger = get_logger("CACHE")
T = TypeVar("T")


class SWRCache:
    def __init__(self, fn: Callable[[], Any], ttl: float, name: str = ""):
        self.fn = fn
        self.ttl = ttl
        self.name = name or getattr(fn, "__name__", "cache")
        self._value: Any = None
        self._at: Optional[float] = None
        self._lock = threading.Lock()
        self._refreshing = False

    def _compute(self) -> Any:
        value = self.fn()
        with self._lock:
            self._value, self._at = value, time.time()
        return value

    def _refresh_bg(self) -> None:
        try:
            self._compute()
        except Exception:  # noqa: BLE001
            logger.exception("后台刷新 %s 失败，继续用旧值", self.name)
        finally:
            with self._lock:
                self._refreshing = False

    def get(self, force: bool = False) -> Any:
        with self._lock:
            has, at = self._at is not None, self._at
        if force or not has:
            return self._compute()
        if time.time() - at >= self.ttl:
            with self._lock:
                start = not self._refreshing
                self._refreshing = True
            if start:
                threading.Thread(target=self._refresh_bg, name=f"swr:{self.name}", daemon=True).start()
        return self._value

    @property
    def cached_at(self) -> Optional[float]:
        return self._at

    def invalidate(self) -> None:
        with self._lock:
            self._at = None
            self._value = None

    def prime(self) -> None:
        """在后台先算一次（启动预热用），不阻塞调用方。"""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_bg, name=f"swr:{self.name}", daemon=True).start()


def swr(ttl: float):
    """装饰无参函数：`@swr(600) def probe(): ...` → `probe()` / `probe(force=True)`。

    被装饰函数上挂着 `.cache`（SWRCache 实例），可 `.cache.invalidate()` / `.cache.prime()`。
    """

    def deco(fn: Callable[[], T]) -> Callable[..., T]:
        cache = SWRCache(fn, ttl, fn.__name__)

        @wraps(fn)
        def wrapper(force: bool = False) -> T:
            return cache.get(force=force)

        wrapper.cache = cache  # type: ignore[attr-defined]
        return wrapper

    return deco
