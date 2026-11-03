"""应用配置：核心字段 + 各功能模块的配置项，统一从 .env 读取。

每个功能可以在 `app/features/<id>/settings.py` 里声明一个 `*Settings(BaseModel)`，
这里把它们全部混入同一个 `Settings`。好处是两头都不变：

  * 环境变量名 = 字段名（大小写不敏感），现有 .env 一行不用改；
  * 调用方照旧 `from app.core.settings import settings` + `settings.<字段>`。

⚠️ 运行时覆盖（如 Mercor 面板切 world 后改写 settings 字段）依赖「调用时读
`settings.xxx`、没有模块级拷贝」——新增读者也要守这条。

公开仓库里缺了某些私有功能目录时，它们的字段自然不存在，核心不受影响。
"""

import importlib
import pkgutil
from functools import lru_cache
from pathlib import Path

import dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

dotenv.load_dotenv()


class CoreSettings(BaseSettings):
    """核心配置（与具体功能无关的部分）。"""

    ntfy_base_url: str = "https://ntfy.sh"
    # 完整 topic = 前缀 + 功能名。用公共 ntfy.sh 时 topic 名就是唯一的访问控制，前缀要够随机。
    ntfy_topic_prefix: str = "myspiders_"
    admin_token: str = ""
    # false = 开发实例：不启动调度器、Discord 长连接与启动钩子（只提供 API 与页面），
    # 避免和生产抢 Telegram 会话、重复推 ntfy、重复写外部世界。
    background_tasks_enabled: bool = True

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_default_db: str = "spider_data"

    log_dir: str = "logs"
    log_filename: str = "spider_system.log"
    default_timezone: str = "Asia/Shanghai"

    # DeepSeek (翻译)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    # 面板对外的根地址（通知里的跳转链接用，如 https://panel.example.com）。
    # 空 = 通知不带跳转链接。
    public_base_url: str = ""

    def public_url(self, path: str = "") -> str | None:
        """拼面板公网链接；`public_base_url` 没配时返回 None（通知就不带跳转）。"""
        base = self.public_base_url.rstrip("/")
        return f"{base}{path}" if base else None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def _feature_settings() -> list[type[BaseModel]]:
    """收集 app/features/*/settings.py 里的 *Settings 类（按包名排序，结果稳定）。"""
    features_dir = Path(__file__).resolve().parent.parent / "features"
    found: list[type[BaseModel]] = []
    for info in sorted(pkgutil.iter_modules([str(features_dir)]), key=lambda i: i.name):
        if not info.ispkg or not (features_dir / info.name / "settings.py").exists():
            continue
        mod = importlib.import_module(f"app.features.{info.name}.settings")
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
                    and obj.__module__ == mod.__name__ and obj.__name__.endswith("Settings")):
                found.append(obj)
    return found


Settings = type("Settings", (CoreSettings, *_feature_settings()), {"__doc__": "核心 + 全部功能模块的配置。"})


@lru_cache
def get_settings() -> CoreSettings:
    """返回全局唯一的 Settings 实例。"""
    return Settings()


settings = get_settings()
