"""启动入口必须在 uvicorn 之前打好 sse-starlette 的退出补丁（见 app/__main__.py）。

否则面板开着时（爬虫状态 SSE 长连接）优雅关闭会一直卡住，生产上表现为每次重启
都被 systemd SIGKILL。用子进程实测：起服务 → 连 SSE → SIGINT → 必须 5 秒内退出。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]



def test_shutdown_with_open_sse_connection_is_fast():
    port = _free_port()
    env = {**os.environ, "BACKGROUND_TASKS_ENABLED": "false"}
    proc = subprocess.Popen([sys.executable, "-m", "app", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("服务没起来")
        sse = socket.create_connection(("127.0.0.1", port))
        sse.sendall(f"GET /api/spiders/events HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        time.sleep(0.5)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=8)
        assert time.monotonic() - t0 < 5
        sse.close()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_launcher_imports_sse_patch_before_uvicorn():
    src = (ROOT / "app" / "__main__.py").read_text(encoding="utf-8")
    assert src.index("import sse_starlette") < src.index("uvicorn.run(")
