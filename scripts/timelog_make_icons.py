"""生成计时 PWA 的桌面图标（一次性脚本，纯标准库）。

    python scripts/timelog_make_icons.py

产出 app/dashboard/timelog-icons/icon-{192,512}.png。图案是暖黑底上一个青色表盘
加一根指针，颜色取自 theme.css 的 --bg / --accent（改配色后重跑本脚本）。
没用 PIL：服务器的 venv 里没有它，为两张图标装一个图像库不值得。

图标按 maskable 设计：图案全落在中心 80% 的安全区内，Android 裁成圆形/圆角方
都不会切到表盘。
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "web" / "public" / "theme.css"
OUT = ROOT / "app" / "dashboard" / "timelog-icons"
SS = 4  # 每像素 4×4 超采样抗锯齿


def _theme_rgb(name: str) -> tuple[int, int, int]:
    m = re.search(rf"--{name}:\s*#([0-9a-fA-F]{{6}})", THEME.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"theme.css 里找不到 --{name}")
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def _coverage(x: float, y: float) -> bool:
    """单位坐标 (0..1) 下该点是否落在前景图案上。"""
    cx = cy = 0.5
    r = math.hypot(x - cx, y - cy)
    ring = 0.26 <= r <= 0.32                       # 表盘外圈
    knob = abs(x - cx) <= 0.04 and 0.12 <= y <= 0.18  # 顶部按钮（秒表）
    stem = abs(x - cx) <= 0.02 and 0.16 <= y <= 0.19
    ang = math.radians(-50)                         # 指针指向右上
    hx, hy = cx + 0.19 * math.sin(-ang), cy - 0.19 * math.cos(-ang)
    hand = _seg_dist(x, y, cx, cy, hx, hy) <= 0.028
    hub = r <= 0.045
    return ring or knob or stem or hand or hub


def render(size: int) -> bytes:
    bg, fg = _theme_rgb("bg"), _theme_rgb("accent")
    rows = bytearray()
    n = SS * SS
    for j in range(size):
        rows.append(0)  # filter: None
        for i in range(size):
            hit = 0
            for sj in range(SS):
                for si in range(SS):
                    if _coverage((i + (si + 0.5) / SS) / size, (j + (sj + 0.5) / SS) / size):
                        hit += 1
            a = hit / n
            rows.extend(round(bg[k] * (1 - a) + fg[k] * a) for k in range(3))
    return _png(size, size, bytes(rows))


def _png(w: int, h: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = OUT / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} B)")


if __name__ == "__main__":
    main()
