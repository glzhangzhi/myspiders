"""启动入口：`python -m app [--host 127.0.0.1] [--port 12312]`。

为什么不用 `uvicorn app.main:app` 命令行（2026-11 排查重启被 SIGKILL 时定位）：
sse-starlette 靠给 `uvicorn.Server.handle_exit` 打补丁来得知「服务要退出了」，
进而结束 SSE 长连接。但 uvicorn 是**先装信号处理器、后导入应用**的 ——
从命令行启动时补丁打得太晚，装上去的是原版处理器，SSE 生成器永远等不到退出
信号；只要面板开着（爬虫状态是一条 SSE 长连接），优雅关闭就卡在
「Waiting for connections to close」，直到 systemd 的 TimeoutStopSec 把进程 SIGKILL。
这里在 uvicorn 启动之前先导入 sse_starlette，补丁就位，关闭耗时 >10s → 0.5s。
"""

from __future__ import annotations

import argparse

import sse_starlette.sse  # noqa: F401  必须先于 uvicorn.run（见模块说明）
import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=12312)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    uvicorn.run("app.main:app", host=a.host, port=a.port, reload=a.reload,
                timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
