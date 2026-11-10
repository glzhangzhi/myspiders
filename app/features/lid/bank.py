"""题库：进程内只读一次 data/questions.json（由 scripts/lid_build.py 生成并提交进仓库）。"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
BANK_FILE = DATA_DIR / "questions.json"
IMG_DIR = DATA_DIR / "img"
IMG_NAME = re.compile(r"^(?:\d{1,3}|HE-\d{1,2})\.png$")


class Bank:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
        data = json.loads(raw)
        self.meta: dict = data["meta"]
        self.questions: list[dict] = data["questions"]
        self.by_id: dict[str, dict] = {q["id"]: q for q in self.questions}
        self.order: dict[str, int] = {q["id"]: i for i, q in enumerate(self.questions)}
        self.key: dict[str, int] = {q["id"]: q["answer"] for q in self.questions}
        self.general_ids = [q["id"] for q in self.questions if q["part"] == "general"]
        self.state_ids = [q["id"] for q in self.questions if q["part"] == "state"]

    def __len__(self) -> int:
        return len(self.questions)


@lru_cache(maxsize=1)
def get_bank() -> Bank:
    return Bank(BANK_FILE.read_bytes())


def image_path(name: str) -> Path | None:
    """白名单校验后的图片路径；不合法或不存在返回 None。"""
    if not IMG_NAME.fullmatch(name):
        return None
    p = IMG_DIR / name
    return p if p.is_file() else None
