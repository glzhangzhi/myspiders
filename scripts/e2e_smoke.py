#!/usr/bin/env python3
"""前端冒烟：用无头 Chromium 打开每个导航页，断言无控制台错误、无失败请求，并截图。

用法：
    python scripts/e2e_smoke.py [--base http://127.0.0.1:8000] [--out /tmp/e2e] [--theme dark|light]

token 从 .env 的 ADMIN_TOKEN 读，预先写进 localStorage。退出码非 0 = 有页面出错。
只读：不点任何按钮，不触发写操作。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _token() -> str:
    env = ROOT / ".env"
    for line in env.read_text("utf-8").splitlines() if env.exists() else []:
        if line.upper().startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default="/tmp/e2e")
    ap.add_argument("--theme", default="dark")
    ap.add_argument("--only", default="", help="逗号分隔的页面 id")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    mods = json.load(urllib.request.urlopen(f"{a.base}/api/modules"))["modules"]
    ids = [n["id"] for m in mods for n in m["nav"]]
    if a.only:
        ids = [i for i in ids if i in a.only.split(",")]

    from playwright.sync_api import sync_playwright

    bad = 0
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(viewport={"width": 1400, "height": 900}, locale="zh-CN", timezone_id="Europe/Berlin")
        ctx.add_init_script(f"localStorage.setItem('myspiders-token', {json.dumps(_token())});"
                            f"localStorage.setItem('myspiders-theme', {json.dumps(a.theme)});")
        for pid in ids:
            page = ctx.new_page()
            errs: list[str] = []
            page.on("console", lambda m: errs.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
            page.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
            page.goto(f"{a.base}/dashboard/{pid}", wait_until="networkidle")
            page.wait_for_timeout(800)
            page.screenshot(path=str(out / f"{pid}.png"))
            status = "OK " if not errs else "ERR"
            bad += bool(errs)
            print(f"{status} {pid}" + "".join(f"\n     {e[:200]}" for e in errs))
            page.close()
        br.close()
    print(f"\n{len(ids) - bad}/{len(ids)} 页正常，截图在 {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
