"""pytest 引导：把项目根加入 sys.path，并让测试进程不启动任何后台任务。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 测试里起 TestClient 会跑 lifespan：不许它启动调度器、连 Telegram / Discord。
os.environ.setdefault("BACKGROUND_TASKS_ENABLED", "false")
