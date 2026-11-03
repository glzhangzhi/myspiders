"""导入 smoke test:捕捉 ModuleNotFoundError / 导入期语法错。

重构删/改 import(如退役 av_refetch)或脚本路径问题,在重启前就能暴露。
"""


def test_import_app_main():
    import app.main

    assert hasattr(app.main, "app")


def test_all_feature_modules_import():
    """每个 app/features/<id>/module.py 都能导入并导出 MODULE（严格模式，失败即抛）。"""
    import os

    os.environ["MYSPIDERS_STRICT_MODULES"] = "1"
    from app.core.registry import discover

    found = discover()
    assert found and all(x.module is not None for x in found), [x for x in found if x.module is None]
