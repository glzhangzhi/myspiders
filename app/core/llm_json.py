"""从大模型回复里取出 JSON 对象（全站共用一份）。

即便开了 JSON mode，也见过模型用 ```json 围栏把 JSON 包起来、或前后夹一句客套话，
所以做三级兜底：整段解析 → 围栏内解析 → 截取最外层大括号。
此前 recipe / story / slack 各抄了一份，2026-11 合并到这里。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def extract_json(text: Optional[str]) -> Optional[Any]:
    """解析成功返回对象（通常是 dict），三级都失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    fenced = _FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except (ValueError, TypeError):
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            pass
    return None


def extract_json_object(text: Optional[str]) -> Optional[dict]:
    """同 extract_json，但只接受 dict（顶层是数组/标量时返回 None）。"""
    obj = extract_json(text)
    return obj if isinstance(obj, dict) else None
