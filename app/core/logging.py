import logging
from pathlib import Path
from typing import Optional

from app.core.settings import settings

_CONFIGURED = False


def configure_logging(log_file: Optional[str] = None):
    """初始化全局 logging，确保只运行一次。"""

    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        filename=log_dir / (log_file or settings.log_filename),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        encoding="utf-8",
    )

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取一个命名 logger，必要时自动先初始化配置。"""

    configure_logging()
    return logging.getLogger(name)
