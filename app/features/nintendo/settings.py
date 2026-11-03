"""Nintendo eShop的配置项（环境变量名与字段名相同，大小写不敏感）。

由 app.core.settings 自动合并进全局 `settings`，读取方式不变：`settings.<字段>`。
"""

from pydantic import BaseModel


class NintendoSettings(BaseModel):
    # 港服 eShop 折扣数据源
    url: str = ""
