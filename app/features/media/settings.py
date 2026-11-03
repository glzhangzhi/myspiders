"""媒体下载的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class MediaSettings(BaseModel):
    # ── 媒体下载（视频号作者 + YouTube 列表）──────────────────────────────
    # 执行端是另一台 Windows 机器上的 WSL：服务器经 ssh 把 deploy/win-media/media_agent.py 推过去再跑。
    # 视频号没有对外接口，作者列表只能借电脑微信的登录态拿（ltaoo/wx_channels_download
    # 的本地 API），所以**检测只在那台电脑开着时发生**，关机期间不检测、不报错。
    # 详见 docs/media.md。
    media_agent_host: str = ""                      # ~/.ssh/config 里的别名；空 = 未配置（检测静默跳过）
    media_agent_python: str = "/root/media-agent/.venv/bin/python"
    media_agent_path: str = "/root/media-agent/agent.py"
    media_ltaoo_url: str = "http://127.0.0.1:2022"  # WSL mirrored 网络，localhost 即 Windows
    media_wx_root_win: str = "D:\\Downloads\\视频号"   # ltaoo 是 Windows 程序，给它 Windows 路径
    media_yt_root: str = "/mnt/d/Downloads/YouTube"  # yt-dlp 跑在 WSL，给它 WSL 路径
    media_yt_cookies: str = ""                      # yt-dlp 的 cookies.txt（WSL 路径），可空
    media_scan_interval_minutes: int = 30
    media_min_free_gb: float = 20.0                 # D 盘低于此值拒绝开始下载
    media_ntfy_topic: str = "media"
