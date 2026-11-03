"""守住 core/push.py 里写下的通知优先级规则。

规则本身在 `app/core/push.py` 的模块 docstring 里，这里是它的**可执行版本**：
文档会过期，测试不会。核心一条 —— **全站不用 `default`**：本机的 default 是
「响+震但锁屏看不到内容」，高打扰低信息，是最差的一档。

用 `ast` 而不是 grep：多行写法（`send_note(\\n  ...\\n  priority=...\\n)`）
grep 根本匹配不到，实测第一次扫就差点漏掉几处。

    .venv/bin/pytest tests/test_push_priority_policy.py -v
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# 会把通知发出去的函数名
_NOTIFY_FUNCS = {"send_note", "send_link", "_notify"}

# 允许的优先级字面量。**刻意不含 "default"**。
_ALLOWED = {"urgent", "high", "low", "min"}

# 白名单：这些位置的 "default" 字面量不是「发通知时选了 default」
#   - ntfy.py 的 _priority_value 是字符串→数字的映射表，必须保留 default 条目
#   - push.py 是仪表盘手动推送接口，优先级由人在表单里选，不该写死
_PRIORITY_MAP = APP / "clients" / "ntfy.py"
_MANUAL_PUSH_ROUTE = APP / "features" / "system" / "push_router.py"


def _iter_notify_calls():
    """遍历 app/ 下所有「发通知」的调用，yield (文件, 行号, priority 字面量或 None)。"""
    for path in sorted(APP.rglob("*.py")):
        if path == _MANUAL_PUSH_ROUTE:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _NOTIFY_FUNCS:
                continue
            # 跳过 core/push.py 里的函数定义自身转发给 client 的那两处
            if path.name == "push.py" and path.parent.name == "core":
                continue
            literal = None
            for kw in node.keywords:
                if kw.arg == "priority" and isinstance(kw.value, ast.Constant):
                    literal = kw.value.value
            yield path, node.lineno, literal


def test_no_default_priority_anywhere():
    """全站不许出现 priority="default"。"""
    offenders = [
        f"{path.relative_to(APP.parent)}:{lineno}"
        for path, lineno, literal in _iter_notify_calls()
        if literal == "default"
    ]
    assert not offenders, (
        "这些调用点用了 default —— 本机上它是「响+震但锁屏看不到内容」，"
        "高打扰低信息。值得打扰用 high，不值得用 low：\n  " + "\n  ".join(offenders)
    )


def test_priority_literals_are_valid():
    """写了 priority 的，字面量必须在允许集合里（拼错会被 ntfy 静默当成 3）。"""
    bad = [
        f"{path.relative_to(APP.parent)}:{lineno} → {literal!r}"
        for path, lineno, literal in _iter_notify_calls()
        if literal is not None and literal not in _ALLOWED
    ]
    assert not bad, (
        f"优先级字面量只能是 {sorted(_ALLOWED)}；拼错不会报错，"
        "会被 _priority_value 静默退回 3(default)：\n  " + "\n  ".join(bad)
    )


def test_notify_helper_defaults_are_non_intrusive():
    """封装函数的 priority 默认值必须挑「不打扰」的一端。

    `priority` 不传 = 不带该字段 = 服务端按 default 处理。所以辅助函数若把默认值
    写成 "default"，等于给将来新增的调用点埋了个「最差档位」的雷。
    """
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in _NOTIFY_FUNCS:
                continue
            if path.name == "push.py" and path.parent.name == "core":
                continue  # 这两个是通用底座，签名允许 Optional[str] = None
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if arg.arg != "priority" or default is None:
                    continue
                if isinstance(default, ast.Constant) and default.value not in ("low", "min"):
                    offenders.append(
                        f"{path.relative_to(APP.parent)}:{node.lineno} "
                        f"默认值={default.value!r}"
                    )
    assert not offenders, (
        "通知辅助函数的 priority 默认值应为 low/min（需要打扰的调用点自己显式写）：\n  "
        + "\n  ".join(offenders)
    )


def test_priority_map_still_covers_all_levels():
    """ntfy 客户端的字符串→数字映射要覆盖五级。

    这张表**必须**保留 default 条目：外部传入（仪表盘手动推送）仍可能给到它。
    这里守的是「表别被人顺手删条目」，与「业务代码不用 default」不矛盾。
    """
    source = _PRIORITY_MAP.read_text(encoding="utf-8")
    for level in ("min", "low", "default", "high", "urgent"):
        assert f'"{level}"' in source, f"_priority_value 映射表缺了 {level}"


def test_policy_doc_exists_in_push_service():
    """规则文档必须留在 push_service 模块 docstring 里。

    这是给「以后加通知的人」看的唯一入口；文档被删掉，上面几条测试就变成了
    没有出处的神秘约束。
    """
    import app.core.push as ps

    doc = ps.__doc__ or ""
    for anchor in ("urgent", "high", "low", "min", "default 是反模式"):
        assert anchor in doc, f"core/push.py docstring 里缺少 {anchor!r} 的说明"
    # 常量也要在，方便 IDE 补全时看到可选值
    assert ps.PRIORITY_URGENT == "urgent"
    assert ps.PRIORITY_HIGH == "high"
    assert ps.PRIORITY_LOW == "low"
    assert ps.PRIORITY_MIN == "min"
    assert not hasattr(ps, "PRIORITY_DEFAULT"), "不该提供 default 常量，等于鼓励使用"
