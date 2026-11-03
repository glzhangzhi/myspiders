"""守住汇总聚合的口径。不碰 DB —— 把 repo 的读方法替换成内存列表。

为什么这层值得测：前端阶段这里踩过**两次**同类错误，都是「同一个数字在
页面多处出现却不相等」：

1. 「固定 vs 可变」漏减退款 → 同一个月统计卡 €2.451、条形图 €2.488
2. 类别占比没跟着「只看可变」的口径走 → 环形图已滤、右边排行表没滤

两次都不是崩溃，是**静悄悄的不一致**，只能靠断言横向比对抓出来。
聚合搬到后端后，这里就是那道防线。

    .venv/bin/pytest tests/test_finance_summary.py -v
"""

from __future__ import annotations

import pytest

from app.features.finance import service as finance_service


def _tx(**kw):
    """造一笔交易。默认是一笔普通可变支出。"""
    d = {
        "id": kw.get("id") or f"t{abs(hash(str(sorted(kw.items())))) % 10**8}",
        "month": "2026-03", "section": "main", "seq": 1,
        "date": "2026-03-05", "amount": -10.0,
        "raw_desc": "", "memo": "", "channel": "card", "bank_cat": "",
        "counter_iban": "", "statement_id": "s1",
        "merchant": "某商家", "kind": "expense", "l1": "餐饮", "l2": "超市生鲜",
        "fixed": False, "excluded": False, "tags": [], "note": "",
        "reviewed": False, "auto": True,
    }
    d.update(kw)
    return d


@pytest.fixture
def fake_repo(monkeypatch):
    """把 list_tx 换成内存数据源，其余不动。"""
    store: list[dict] = []

    def _list_tx(months=None, **_):
        return [t for t in store if not months or t["month"] in months]

    monkeypatch.setattr(finance_service.finance_repo, "list_tx", _list_tx)
    return store


# ── 退款必须冲减原类别，且在每一处都冲减 ──


def test_退款冲减原类别而不是算作收入(fake_repo):
    fake_repo += [
        _tx(amount=-30.0, l1="通信数码", merchant="Spotify"),
        _tx(amount=12.99, kind="refund", l1="通信数码", merchant="Spotify"),
    ]
    s = finance_service.summary(["2026-03"], only_var=False)
    assert s["totals"]["total"] == pytest.approx(17.01)
    cat = {c["l1"]: c["total"] for c in s["categories"]}
    assert cat["通信数码"] == pytest.approx(17.01)


def test_固定项的退款要从固定里减而不是从可变里减(fake_repo):
    """真实踩过的 bug：3 月 Spotify 退了 12,99，而 Spotify 被标为固定支出。

    `fixed` 只累加支出、不冲减退款的话，`var = total - fixed` 会把这 12,99
    算到可变头上 —— 统计卡显示 3277,12，环形图算出 3290,11，同一页两个数。
    """
    fake_repo += [
        _tx(amount=-6.99, fixed=True, l1="通信数码", merchant="Spotify"),
        _tx(amount=12.99, kind="refund", fixed=True, l1="通信数码", merchant="Spotify"),
        _tx(amount=-100.0, l1="餐饮", merchant="Lidl"),
    ]
    s = finance_service.summary(["2026-03"], only_var=False)
    t = s["totals"]
    assert t["fixed"] == pytest.approx(-6.0)      # 6.99 支出 − 12.99 退款
    assert t["var"] == pytest.approx(100.0)       # 可变不该被那 12.99 污染
    assert t["total"] == pytest.approx(94.0)
    assert t["fixed"] + t["var"] == pytest.approx(t["total"])


def test_各视图的合计必须两两相等(fake_repo):
    """同一个数字在页面多处呈现时必须相等 —— 这是整页可信度的底线。"""
    fake_repo += [
        _tx(amount=-100.0, l1="餐饮", merchant="Lidl", date="2026-03-01"),
        _tx(amount=-50.0, l1="购物", merchant="Amazon", date="2026-03-02"),
        _tx(amount=-1100.0, fixed=True, l1="住房", merchant="房东", date="2026-03-01"),
        _tx(amount=10.0, kind="refund", l1="购物", merchant="Amazon", date="2026-03-03"),
    ]
    s = finance_service.summary(["2026-03"], only_var=True)
    scoped = s["totals"]["scoped"]
    assert scoped == pytest.approx(140.0)                                  # 100 + 50 − 10
    assert sum(c["total"] for c in s["categories"]) == pytest.approx(scoped)
    assert sum(r["total"] for r in s["stack"]) == pytest.approx(scoped)
    assert sum(m["total"] for m in s["merchants"]) == pytest.approx(scoped)
    assert sum(s["daily"].values()) == pytest.approx(scoped)
    # 二级分类合计也要等于所属大类
    for c in s["categories"]:
        assert sum(x["total"] for x in c["subs"]) == pytest.approx(c["total"])


# ── only_var 口径 ──


def test_只看可变时固定支出从所有分布视图消失(fake_repo):
    fake_repo += [
        _tx(amount=-1100.0, fixed=True, l1="住房", merchant="房东"),
        _tx(amount=-20.0, l1="餐饮", merchant="Lidl"),
    ]
    s = finance_service.summary(["2026-03"], only_var=True)
    assert [c["l1"] for c in s["categories"]] == ["餐饮"]
    assert [m["merchant"] for m in s["merchants"]] == ["Lidl"]
    assert "住房" not in s["stack"][0]["parts"]
    assert sum(s["daily"].values()) == pytest.approx(20.0)
    # 但总额卡片仍要给出全貌，否则用户不知道自己滤掉了多少
    assert s["totals"]["total"] == pytest.approx(1120.0)
    assert s["totals"]["fixed"] == pytest.approx(1100.0)
    assert s["fixed_names"] == ["房东"]


def test_住房里的一次性维修属于可变不该被滤掉(fake_repo):
    """住房 ≠ 固定。3 月那笔 211,92 的通下水道是一次性的，该留在可变里。
    判据是 `fixed` 标记，不是大类名 —— 按大类名硬编码就会误杀。"""
    fake_repo += [
        _tx(amount=-1100.0, fixed=True, l1="住房", l2="房租", merchant="房东"),
        _tx(amount=-211.92, fixed=False, l1="住房", l2="家居维修", merchant="Abfluss-AS"),
    ]
    s = finance_service.summary(["2026-03"], only_var=True)
    assert [c["l1"] for c in s["categories"]] == ["住房"]
    assert s["categories"][0]["total"] == pytest.approx(211.92)


# ── 排除、非消费、Space ──


def test_excluded的交易不进任何统计(fake_repo):
    fake_repo += [
        _tx(amount=-100.0, merchant="算的"),
        _tx(amount=-999.0, excluded=True, merchant="不算的"),
    ]
    s = finance_service.summary(["2026-03"], only_var=False)
    assert s["totals"]["total"] == pytest.approx(100.0)
    assert [m["merchant"] for m in s["merchants"]] == ["算的"]


def test_非消费不计入支出(fake_repo):
    fake_repo += [
        _tx(amount=-100.0),
        _tx(amount=-500.0, kind="transfer", l1="非消费", l2="储蓄划转", merchant="Savings"),
    ]
    s = finance_service.summary(["2026-03"], only_var=False)
    assert s["totals"]["total"] == pytest.approx(100.0)
    assert {n["kind"] for n in s["nonconsume"]} == {"储蓄划转"}


def test_非消费只统计主账户那条腿(fake_repo):
    """一笔「主账户 → Savings Space」在 PDF 里是两条记录（主 -54,35 / Space +54,35）。
    两条都算净额恒为 0，表格会显示「储蓄划转净额 €0,00」—— 看起来像一分钱
    没存，而真实情况是主账户净流出 54,35。"""
    fake_repo += [
        _tx(amount=-54.35, kind="transfer", section="main",
            l1="非消费", l2="储蓄划转", merchant="Savings 储蓄 Space"),
        _tx(amount=54.35, kind="transfer", section="space",
            l1="非消费", l2="储蓄划转", merchant="主账户"),
    ]
    s = finance_service.summary(["2026-03"], only_var=False)
    row = s["nonconsume"][0]
    assert row["net"] == pytest.approx(-54.35)
    assert row["n"] == 1


# ── 跨月 ──


def test_按月切分且只算选中的月份(fake_repo):
    fake_repo += [
        _tx(month="2026-03", amount=-100.0, date="2026-03-05"),
        _tx(month="2026-04", amount=-70.0, date="2026-04-05"),
        _tx(month="2026-05", amount=-30.0, date="2026-05-05"),
    ]
    s = finance_service.summary(["2026-03", "2026-04"], only_var=False)
    assert s["by_month"] == {"2026-03": 100.0, "2026-04": 70.0}
    assert s["totals"]["total"] == pytest.approx(170.0)
    assert s["totals"]["per_month"] == pytest.approx(85.0)
    assert "2026-05-05" not in s["daily"]


def test_日历按iso日期聚合跨月不撞车(fake_repo):
    """早期按「几号」做键，3 月 5 日和 4 月 5 日会被加到一起。"""
    fake_repo += [
        _tx(month="2026-03", amount=-10.0, date="2026-03-05"),
        _tx(month="2026-04", amount=-20.0, date="2026-04-05"),
    ]
    s = finance_service.summary(["2026-03", "2026-04"], only_var=False)
    assert s["daily"]["2026-03-05"] == pytest.approx(10.0)
    assert s["daily"]["2026-04-05"] == pytest.approx(20.0)


def test_空月份不崩(fake_repo):
    s = finance_service.summary([], only_var=True)
    assert s["totals"]["total"] == 0
    assert s["categories"] == [] and s["merchants"] == []
    assert s["totals"]["avg_tx"] == 0        # 不能除零


# ── 标签 ──


def test_标签汇总跨类别(fake_repo):
    """标签与类别正交：一次搬家会横跨购物、交通、住房。"""
    fake_repo += [
        _tx(amount=-100.0, l1="购物", tags=["搬家"]),
        _tx(amount=-50.0, l1="交通", tags=["搬家", "报销"]),
        _tx(amount=-20.0, l1="餐饮", tags=[]),
    ]
    s = finance_service.summary(["2026-03"], only_var=False)
    tags = {t["tag"]: t for t in s["tags"]}
    assert tags["搬家"]["total"] == pytest.approx(150.0)
    assert tags["搬家"]["n"] == 2
    assert tags["报销"]["total"] == pytest.approx(50.0)


def test_import_without_pdftotext_returns_503(monkeypatch):
    """没装 poppler-utils 时给出可操作的提示，而不是裸 500。"""
    from fastapi.testclient import TestClient

    from app.core.settings import settings
    from app.features.finance import service as fs
    from app.main import app

    monkeypatch.setattr(settings, "admin_token", "t")
    monkeypatch.setattr(fs, "ensure_seeded", lambda: None)

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "pdftotext")

    monkeypatch.setattr(fs, "import_pdf", boom)
    r = TestClient(app).post("/api/finance/import", headers={"x-token": "t"},
                             files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 503
    assert "poppler" in r.json()["detail"]
