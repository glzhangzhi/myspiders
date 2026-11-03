"""计时的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class TimelogSettings(BaseModel):
    # ── 计时 (timelog) ─────────────────────────────────────────
    # 提醒的 topic（完整名 = 前缀 + 它，手机要订阅 <前缀>timelog）。
    timelog_ntfy_topic: str = "timelog"
