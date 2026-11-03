"""Joybuy 降价监控的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class JoybuySettings(BaseModel):
    # Joybuy 单品降价监控 (www.joybuy.de)
    # 盯住一个 SKU，跌破阈值推 ntfy。
    #
    # ⚠️ **必须带登录态 cookie**，否则整站（含首页）都 307 到
    # `/user/risk/pic` 风控页 —— 而且那个页面返回的是 **200**，只有 <title>
    # 写着「403 Verboten」，只看状态码会把拦截当成成功。
    # 实测唯一必需的 cookie 是 `mfs_session`（单独带它就够；`pin` /
    # `3AB9D23F7A4B3C9B` / `__uj` 单独带全部被拦）；`h5st` 签名**不需要**。
    #
    # cookie 本体**存 Mongo 不存这里**（见 repositories/joybuy_watch.py）：
    # 它是会话级的、会过期，而改 .env 要重启服务。这里只留启动兜底值。
    joybuy_cookie: str = ""
    joybuy_sku: str = "102790346"
    joybuy_product_url: str = (
        "https://www.joybuy.de/dp/"
        "joyfurnos-dreifachkompartiment-recyclingm%C3%BClleimer-3-x-18/102790346"
    )
    # 阈值可在仪表盘热改（存 Mongo），这里是库里没记录时的默认值
    joybuy_price_threshold: float = 10.0
    joybuy_ntfy_topic: str = "joybuy"
    joybuy_scan_interval_minutes: int = 60
