"""经 ssh 调 3060ti WSL 上的 media agent。

## 为什么是「服务器 ssh 过去」而不是「电脑上跑个服务连回来」

- `wsl-3060ti` 这条 ssh 通道已经存在（tailscale serve 2200 → WSL sshd），
  面板继续只绑 127.0.0.1，**不需要开任何新的入站端口**；
- 电脑那边不需要常驻任何东西：WSL 实例由 `\\DSH-WSL-Boot` 计划任务在登录时钉住，
  sshd 随 systemd 起来，就够了。

## agent 只有一份

运行长任务前先把 `deploy/win-media/media_agent.py` 推过去（`cat > tmp && mv`，
几 KB）。仓库是唯一来源，那边的文件只是缓存 —— 改了 agent 不需要「记得去部署」。

## 读输出必须另起线程

与 tavern_sync 同一个坑：agent 在等 ltaoo / yt-dlp 时可能很久一个字节都不输出，
主线程卡在 readline 上就永远走不到超时和「停止」判断。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Callable, Iterator, Optional

from app.core.cache import SWRCache
from app.core.paths import REPO_ROOT
from app.core.logging import get_logger
from app.core.settings import settings

logger = get_logger("media_agent")

AGENT_SRC = REPO_ROOT / "deploy" / "win-media" / "media_agent.py"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]

# 探测一次要几秒（关机时等满 ConnectTimeout=8）。过期后先回旧值、后台重探 ——
# 打开页面不必等 ssh；需要实时结论的地方（下载前、「重新探测」按钮）传 force=True。
_PING_TTL = 60.0


class AgentOffline(Exception):
    """连不上那台电脑。这是**常态**（关机了），不是错误 —— 调用方不该推送、不该报红。"""


def agent_config() -> dict:
    """每次调用时读 settings（全站约定：不在模块级拷贝配置）。"""
    return {
        "ltaoo_url": settings.media_ltaoo_url,
        "wx_root_win": settings.media_wx_root_win,
        "yt_root": settings.media_yt_root,
        "yt_cookies": settings.media_yt_cookies,
    }


def _ssh(*remote: str) -> list[str]:
    if not settings.media_agent_host:
        # 没配执行端：与「那台电脑关着」同等对待 —— 静默跳过，不报错、不推送
        raise AgentOffline("未配置 MEDIA_AGENT_HOST")
    return ["ssh", *SSH_OPTS, settings.media_agent_host, *remote]


def _is_offline(rc: int, stderr: str) -> bool:
    s = (stderr or "").lower()
    return rc == 255 and any(k in s for k in (
        "timed out", "connection refused", "no route", "could not resolve",
        "network is unreachable", "connection closed", "host is down", "kex_exchange",
        "connection reset",
    ))


def push_agent() -> None:
    """把仓库里的 agent 推过去。原子替换：正在跑的旧进程已把文件读进内存，不受影响。"""
    src = AGENT_SRC.read_bytes()
    dst = settings.media_agent_path
    cmd = _ssh(f"mkdir -p $(dirname {dst}) && cat > {dst}.tmp && mv {dst}.tmp {dst}")
    p = subprocess.run(cmd, input=src, capture_output=True, timeout=30)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace")
        if _is_offline(p.returncode, err):
            raise AgentOffline(err.strip()[:200])
        raise RuntimeError(f"推送 agent 失败：{err.strip()[:300]}")


def run_once(cmd: str, payload: Optional[dict] = None, timeout: float = 60) -> list[dict]:
    """短命令：跑完收齐全部事件返回。"""
    body = json.dumps({"config": agent_config(), **(payload or {})}).encode()
    try:
        p = subprocess.run(_ssh(settings.media_agent_python, settings.media_agent_path, cmd),
                           input=body, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"agent {cmd} 超过 {timeout:.0f} 秒没返回")
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode == 255 or (p.returncode != 0 and not p.stdout):
        if _is_offline(p.returncode, err):
            raise AgentOffline(err.strip()[:200])
        if "No such file" in err or "can't open file" in err:
            push_agent()
            return run_once(cmd, payload, timeout)
        raise RuntimeError(f"agent {cmd} 失败（rc={p.returncode}）：{err.strip()[:300]}")
    return _parse_lines(p.stdout.decode("utf-8", "replace").splitlines())


def _parse_lines(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"ev": "log", "msg": line[:300]})
    return out


def _ping_now() -> dict:
    try:
        evs = run_once("ping", timeout=40)
        p = next((e for e in evs if e.get("ev") == "ping"), None)
        end = next((e for e in evs if e.get("ev") == "end"), {})
        if p is None:
            # agent 文件不存在或是旧版本，推一份再试一次
            push_agent()
            evs = run_once("ping", timeout=40)
            p = next((e for e in evs if e.get("ev") == "ping"), None)
        value = {"online": True, **(p or {}), "error": end.get("error")}
        value.pop("ev", None)
    except AgentOffline:
        value = {"online": False}
    except Exception as e:  # noqa: BLE001
        logger.warning("media agent ping 失败：%s", e)
        value = {"online": True, "error": str(e)[:300]}
    return value


_ping_cache = SWRCache(_ping_now, _PING_TTL, "media_ping")


def ping(force: bool = False) -> dict:
    """那台电脑现在的状态。离线返回 `{"online": False}`，不抛。"""
    return _ping_cache.get(force=force)


def invalidate_ping() -> None:
    _ping_cache.invalidate()


class AgentRun:
    """一次长任务（scan / download）。逐行吐事件，支持超时与停止。"""

    def __init__(self, cmd: str, payload: dict, idle_timeout: float = 1800):
        push_agent()
        self.cmd = cmd
        self.idle_timeout = idle_timeout
        body = json.dumps({"config": agent_config(), **payload}).encode()
        self.proc = subprocess.Popen(
            _ssh(settings.media_agent_python, settings.media_agent_path, cmd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert self.proc.stdin is not None
        self.proc.stdin.write(body)
        self.proc.stdin.close()
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self._q.put(raw.decode("utf-8", "replace"))
        self._q.put(None)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._stderr.append(raw.decode("utf-8", "replace").rstrip())
            if len(self._stderr) > 50:
                del self._stderr[:25]

    def events(self, should_stop: Callable[[], bool] = lambda: False) -> Iterator[dict]:
        last = time.monotonic()
        while True:
            if should_stop():
                return
            try:
                line = self._q.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - last > self.idle_timeout:
                    self.kill()
                    yield {"ev": "end", "status": "failed",
                           "error": f"agent {self.idle_timeout:.0f} 秒没有任何输出，已断开"}
                    return
                continue
            if line is None:
                self.proc.wait(timeout=10)
                rc = self.proc.returncode
                err = "\n".join(self._stderr[-5:])
                if _is_offline(rc, err) or rc == 255:
                    yield {"ev": "end", "status": "failed", "offline": True,
                           "error": "与 3060ti 的连接断开了（关机/睡眠/断网？）" + (f"：{err[:200]}" if err else "")}
                else:
                    yield {"ev": "end", "status": "failed",
                           "error": f"agent 意外退出（rc={rc}）" + (f"：{err[:300]}" if err else "")}
                return
            last = time.monotonic()
            for ev in _parse_lines([line]):
                yield ev
                if ev.get("ev") == "end":
                    return

    def kill(self) -> None:
        """只断本地 ssh。远端 agent 收到 SIGHUP 会自己收尾（杀 yt-dlp 进程组、暂停 ltaoo 任务）。"""
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass


def stop_remote() -> list[dict]:
    return run_once("stop", timeout=30)
