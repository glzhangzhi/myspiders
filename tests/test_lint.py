"""app/ 与 scripts/ 不许有 pyflakes 报的「未定义名字」，未使用导入也要清干净。

导入期/语法期检查拦不住拼错的变量名（只有那一行真跑时才炸）；未使用导入是
重构后的残渣，攒多了会掩盖真正的依赖关系。故 pyflakes 全量过一遍。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 有意为之的少数例外：(文件后缀, 消息片段)
ALLOW = [
    ("app/__main__.py", "'sse_starlette.sse' imported but unused"),   # 副作用导入（打补丁）
    ("assigned to but never used", ""),                               # 局部变量留作可读性，不拦
    ("f-string is missing placeholders", ""),
]


def test_pyflakes_clean():
    out = subprocess.run([sys.executable, "-m", "pyflakes", "app", "scripts", "tests"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    bad = [l for l in out.splitlines()
           if not any(a in l and b in l for a, b in ALLOW)]
    assert not bad, "\n".join(bad)
