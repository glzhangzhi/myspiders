#!/usr/bin/env python3
"""给所有无路径参数的 GET 接口计时，输出 Markdown 表格（慢的在前）。

用法:
    python scripts/bench_endpoints.py [--base http://127.0.0.1:12312] [--runs 3] [--top 30]

token 从 .env 的 ADMIN_TOKEN 读，作为 x-token 头发送。每个接口跑 runs 次，
第一次算「冷」，其余取中位数算「热」。会跳过 SSE / 导出 / 明显有副作用的路径。
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKIP = ("events", "stream", "export", "/sse", "/ping", "orphans", "/wx/")


def _token() -> str:
    env = ROOT / ".env"
    for line in env.read_text("utf-8").splitlines() if env.exists() else []:
        if line.upper().startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get("ADMIN_TOKEN", "")


def _paths() -> list[str]:
    from app.main import app

    out = []
    for r in app.routes:
        methods = getattr(r, "methods", set()) or set()
        path = getattr(r, "path", "")
        if "GET" not in methods or "{" in path or any(s in path for s in SKIP):
            continue
        out.append(path)
    return sorted(set(out))


def _hit(base: str, path: str, token: str) -> tuple[float, int, int]:
    req = urllib.request.Request(base + path, headers={"x-token": token, "Accept-Encoding": "gzip"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        body, code = b"", e.code
    except Exception:
        body, code = b"", 0
    return time.perf_counter() - t0, code, len(body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:12312")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    token = _token()
    rows = []
    for p in _paths():
        times, code, size = [], 0, 0
        for _ in range(max(1, a.runs)):
            t, code, size = _hit(a.base, p, token)
            times.append(t)
        warm = statistics.median(times[1:]) if len(times) > 1 else times[0]
        rows.append((warm, times[0], code, size, p))
    rows.sort(reverse=True)
    print("| 热(ms) | 冷(ms) | code | bytes | path |\n|---:|---:|---:|---:|---|")
    for warm, cold, code, size, p in rows[: a.top]:
        print(f"| {warm*1000:.0f} | {cold*1000:.0f} | {code} | {size} | {p} |")
    print(f"\n共 {len(rows)} 个接口；热态 >200ms 的 {sum(1 for r in rows if r[0] > .2)} 个")


if __name__ == "__main__":
    main()
