"""core/cache.SWRCache：首次同步、过期返回旧值并后台刷新、并发只刷一次。"""

from __future__ import annotations

import threading
import time

import pytest

from app.core.cache import SWRCache, swr


def test_first_call_is_synchronous_then_cached():
    calls = []
    c = SWRCache(lambda: calls.append(1) or len(calls), ttl=60)
    assert c.get() == 1
    assert c.get() == 1
    assert len(calls) == 1


def test_stale_returns_old_value_and_refreshes_in_background():
    gate = threading.Event()
    n = {"v": 0}

    def fn():
        n["v"] += 1
        if n["v"] > 1:
            gate.wait(2)
        return n["v"]

    c = SWRCache(fn, ttl=0.01)
    assert c.get() == 1
    time.sleep(0.02)
    t0 = time.perf_counter()
    assert c.get() == 1                     # 旧值，立即返回
    assert time.perf_counter() - t0 < 0.5
    assert c.get() == 1                     # 刷新进行中：不再起第二个
    c.ttl = 3600                            # 刷新完后不再过期，便于断言
    gate.set()
    for _ in range(200):
        if c.get() == 2:
            break
        time.sleep(0.01)
    assert c.get() == 2
    assert n["v"] == 2                      # 期间多次 get 只触发了一次后台刷新


def test_force_recomputes_synchronously():
    n = {"v": 0}

    @swr(3600)
    def probe():
        n["v"] += 1
        return n["v"]

    assert probe() == 1
    assert probe(force=True) == 2
    assert probe() == 2


def test_first_call_error_propagates_but_bg_error_keeps_old_value():
    state = {"fail": True}

    def fn():
        if state["fail"]:
            raise RuntimeError("down")
        return "ok"

    c = SWRCache(fn, ttl=0.01)
    with pytest.raises(RuntimeError):
        c.get()
    state["fail"] = False
    assert c.get() == "ok"
    state["fail"] = True
    time.sleep(0.02)
    assert c.get() == "ok"
    time.sleep(0.05)
    assert c.get() == "ok"
