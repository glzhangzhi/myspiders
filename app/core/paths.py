"""项目内的固定路径（全站唯一出处）。

模块挪进 app/features/<id>/ 之后，`Path(__file__).parent.parent` 这类相对推导
会随目录深度悄悄指错地方，所以一律从这里取。
"""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
DATA_DIR = APP_DIR / "data"            # 运行时小文件（状态快照等）
DASHBOARD_DIR = APP_DIR / "dashboard"  # 前端静态文件
