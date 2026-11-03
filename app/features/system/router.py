"""系统健康与运维 API — 供仪表盘「系统」标签与页脚重启按钮。"""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, BackgroundTasks, Depends

from app.core.security import require_admin_token
from app.features.system.health import get_health

router = APIRouter(prefix="/api/system", tags=["System"])


@router.get("/health", summary="磁盘/MongoDB/备份 统计")
def system_health(_: str = Depends(require_admin_token)):
    # 同步函数 → FastAPI 在线程池执行,du/rclone 等阻塞调用不影响事件循环
    return get_health()


@router.get("/dsh-url", summary="当前 dsh-web 的带 token 登录链接")
def dsh_url(_: str = Depends(require_admin_token)):
    """调 /usr/local/bin/dsh-url(从 dsh-web 本次启动的 journal 里取 token)。

    token 每次 dsh-web 重启都会换,所以每次现取、不缓存。
    """
    try:
        r = subprocess.run(["/usr/local/bin/dsh-url"], capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        return {"url": None, "error": str(e)}
    if r.returncode != 0:
        return {"url": None, "error": (r.stderr or r.stdout).strip() or f"exit {r.returncode}"}
    return {"url": r.stdout.strip(), "error": None}


# ── 服务重启 ──


def _do_restart():
    """在后台延迟执行重启，确保 HTTP 响应先返回。"""
    import time
    time.sleep(1)
    # 服务由 systemd 托管（myspider.service）。--no-block：把 restart 任务交给 systemd 后
    # 立即返回，避免本进程在 systemctl 等待期间被 SIGTERM 杀掉而中断重启。
    # ubuntu 已配置免密 sudo；用 -n 以防万一需要密码时挂起。
    subprocess.run(
        ["sudo", "-n", "systemctl", "--no-block", "restart", "myspider"],
        capture_output=True, timeout=15,
    )


@router.post(
    "/restart-service",
    summary="重启 myspider 服务",
    dependencies=[Depends(require_admin_token)],
)
def restart_service(bg: BackgroundTasks):
    """通过 systemctl 重启 myspider.service（由 systemd 托管）。"""
    bg.add_task(_do_restart)
    return {"message": "服务将在 1 秒后重启"}
