"""守住「重导 PDF 不冲掉人工标注」这条保证 —— 用 ast 扫描，不连 DB。

为什么不用真 Mongo：CI 和开发机不一定有库，而这条规则的**本质是代码结构**
（导入路径只 $set 机器字段、人工字段走 $setOnInsert），静态就能查。
端到端的行为验证另有真库跑过一次。

为什么要盯这么紧：用户花两小时逐条确认完一个月，一次「重新导入」就该原样
保留。菜谱模块踩过同构的坑（`prev_snapshot` 被同一次 upsert 抹掉），根因
都是 **用 model_dump() 全量 $set**。

    .venv/bin/pytest tests/test_finance_repo_fields.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.features.finance.models import Transaction
from app.features.finance.repo import _MACHINE, _USER_OWNED

_SRC = Path(__file__).resolve().parent.parent / "app" / "features" / "finance" / "repo.py"


def test_两个字段清单不重叠():
    """一个字段只能归一方。同时出现在两边时，$set 会盖掉 $setOnInsert，
    Mongo 甚至会直接报 conflict —— 但更坏的情况是它静静生效、人工值被覆盖。"""
    overlap = set(_MACHINE) & set(_USER_OWNED)
    assert not overlap, f"字段归属冲突：{overlap}"


def test_模型里每个字段都有归属():
    """新加字段时必须显式选边。漏选 = 重导时既不更新也不保留，
    行为取决于 upsert 的偶然细节，是最难查的一类 bug。"""
    known = set(_MACHINE) | set(_USER_OWNED) | {"id", "created_at", "updated_at"}
    missing = set(Transaction.model_fields) - known
    assert not missing, f"这些字段既不在 _MACHINE 也不在 _USER_OWNED：{missing}"


@pytest.mark.parametrize("field", [
    "l1", "l2", "kind", "merchant", "fixed", "excluded", "tags", "note", "reviewed",
])
def test_人工标注字段受保护(field):
    """逐个钉死。有人图省事把某个字段挪进 _MACHINE 时，这里会亮。"""
    assert field in _USER_OWNED


@pytest.mark.parametrize("field", ["raw_desc", "amount", "date", "memo", "month"])
def test_银行事实字段以pdf为准(field):
    """重导的语义是「以银行为准重新对一遍」，这些字段该被刷新。"""
    assert field in _MACHINE


def _import_fn() -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "import_transactions":
            return node
    raise AssertionError("找不到 import_transactions")


def test_导入路径必须用setoninsert写人工字段():
    """用 ast 而不是 grep：多行写法 grep 匹配不到。

    只要导入路径里出现 `$setOnInsert`，就说明人工字段走的是「仅新建时写入」。
    """
    src = ast.unparse(_import_fn())
    assert "$setOnInsert" in src, "导入路径没有 $setOnInsert —— 人工标注会被重导覆盖"


def test_导入路径不得全量set():
    """`$set: doc`（整个 model_dump 结果）是那个经典坑的形态。
    必须是挑出来的机器字段子集。"""
    fn = _import_fn()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "$set":
                # $set 的值必须是变量（machine），而不是 model_dump() 调用
                assert not isinstance(v, ast.Call), "$set 直接挂了一个调用结果，疑似全量写入"
                if isinstance(v, ast.Name):
                    assert v.id != "doc", "$set 用了完整的 doc —— 会覆盖人工字段"
