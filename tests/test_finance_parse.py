"""守住 N26 对账单解析器的版式假设。

解析器是典型的「高风险纯函数」：不碰 DB、不发网络，但一旦 N26 改模板，
正则会**静默少抓一笔**而不报错 —— 钱无声消失在统计里，比崩溃难发现得多。

因此真正的防线是 `reconcile()`（期初 + 本期合计 = 期末）。这里用最小的
合成版式跑通它，并逐条钉死几个曾经踩过或容易踩的坑：

- 页眉页脚在每一页重复，不滤掉会被当成交易的补充行混进备注
- `Space Kontoauszug` 之后是另一本账，与主账户的 `An Savings` 是同一笔钱的两条腿
- 金额是德式写法（`1.100,00`），千分位点与小数逗号不能搞反
- 商家名里的随机订单号必须剥掉，否则规则永远沉淀不下来

    .venv/bin/pytest tests/test_finance_parse.py -v
"""

from __future__ import annotations

import pytest

from app.features.finance.parse import RE_TX, classify, parse_text, reconcile, _num

# 个人规则不在代码里（见 parse.RULES 上方说明），测试用一份示例配置
_PERSONAL = (
    '[["^(An|Von) Savings$", "Savings 储蓄 Space", "非消费", "储蓄划转", false],'
    ' ["^Acme Analytics$", "Acme 工资", "收入", "工资", false],'
    ' ["^Erika Vermieter$", "房东 Erika", "住房", "房租", true]]'
)


@pytest.fixture(autouse=True)
def _personal_rules(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "finance_personal_rules", _PERSONAL)


def test_个人规则排在通用规则前面且格式错误不致命(monkeypatch):
    from app.core.settings import settings

    assert _mk("An Savings")["merchant"] == "Savings 储蓄 Space"
    monkeypatch.setattr(settings, "finance_personal_rules", "not json")
    assert _mk("An Savings")["l1"] == "未分类"


# ── 金额解析：德式写法 ──


@pytest.mark.parametrize("raw,want", [
    ("-1.100,00", -1100.0),
    ("+2.472,98", 2472.98),
    ("-9,47", -9.47),
    ("-1.140,68", -1140.68),
    ("+0,01", 0.01),
])
def test_德式金额千分位点与小数逗号不能搞反(raw, want):
    assert _num(raw) == want


# ── 交易行正则 ──


def test_交易行按两个以上空格切分而非单空格():
    # 商家名自带单空格（`WEG Musterstrasse 1`），按单空格切会把它拦腰截断
    m = RE_TX.match("WEG Musterstrasse 1        01.03.2026   -200,00€")
    assert m is not None
    assert m["desc"].strip() == "WEG Musterstrasse 1"
    assert m["date"] == "01.03.2026"
    assert m["amt"] == "-200,00"


def test_没有货币符号的行不是交易行():
    assert RE_TX.match("Wertstellung 01.03.2026") is None
    assert RE_TX.match("Dein alter Kontostand      +2.472,98") is None


# ── 端到端：合成一份最小对账单 ──

_HEADER = """Kontoauszug Nr. 03/2026
01.03.2026 bis 31.03.2026



Beschreibung                                    Verbuchungsdatum        Betrag
"""
_FOOTER = """

MAX MUSTERMANN                                                      Erstellt am
Musterstraße 1, 12345 Musterstadt                      25.09.2026
IBAN: DE00100110010000000001 • BIC: NTSBDEB1XXX                   Nr. 03/2026
                                                                        1 / 4
"""

_STATEMENT = _HEADER + """WEG Musterstrasse 1                          01.03.2026   -200,00€
Belastungen
IBAN: DE00500502010000000002 • BIC: HELADEF1822
Hausgeld Wohnung 1.OG / Musterstrasse 1
Wertstellung 01.03.2026

Lidl sagt Danke                                    02.03.2026     -9,47€
Mastercard • Lebensmittel
Wertstellung 02.03.2026

An Savings                                             03.03.2026    -54,35€
Wertstellung 03.03.2026

Acme Analytics                                 30.03.2026 +2.472,98€
Gutschriften
IBAN: DE00500400000000000003 • BIC: COBADEFF
03.2026
Wertstellung 30.03.2026
""" + _FOOTER + """
Dein alter Kontostand                             +1.000,00€
Dein neuer Kontostand                             +3.209,16€

Space Kontoauszug Nr. 03/2026
Space: Savings

Von Main Account                                   03.03.2026    +54,35€
Wertstellung 03.03.2026

Spaces Zusammenfassung Nr. 03/2026
Space: Savings
Dein alter Kontostand                               +400,00€
Dein neuer Kontostand                               +454,35€
""" + _FOOTER


@pytest.fixture
def stmt():
    """直接喂文本给 `parse_text`，绕过 pdftotext。

    测的是版式解析这段逻辑，不是 poppler 装没装 —— 真跑 pdftotext 会让这个
    测试在没装 poppler 的机器上变成假阴性。`parse_text` 从 `parse_pdf` 里
    拆出来正是为了这条缝。
    """
    return parse_text(_STATEMENT, fallback_name="statement-2026-03")


def test_四笔主账户加一笔space全部抓到(stmt):
    txs = stmt["transactions"]
    assert len(txs) == 5
    assert sum(1 for t in txs if t["section"] == "main") == 4
    assert sum(1 for t in txs if t["section"] == "space") == 1


def test_余额勾稽是解析没漏的唯一硬证明(stmt):
    rec = {r["section"]: r for r in reconcile(stmt)}
    assert rec["main"]["ok"], rec["main"]
    assert rec["space"]["ok"], rec["space"]
    assert rec["main"]["sum"] == pytest.approx(2209.16)


def test_漏掉一笔会被勾稽抓出来(stmt):
    """这才是这套测试存在的理由 —— 少一笔时必须报错，而不是静静少算。"""
    stmt["transactions"] = [t for t in stmt["transactions"] if t["amount"] != -9.47]
    rec = {r["section"]: r for r in reconcile(stmt)}
    assert not rec["main"]["ok"]
    assert rec["main"]["diff"] == pytest.approx(9.47)


def test_页眉页脚不会混进交易备注(stmt):
    """页眉每页重复出现，滤不掉就会挂到上一笔交易的 memo 上。"""
    for t in stmt["transactions"]:
        memo = t["memo"]
        assert "MAX MUSTERMANN" not in memo
        assert "Erstellt am" not in memo
        assert "Kontoauszug" not in memo
        assert "/ 4" not in memo


def test_渠道与银行类别各归各位(stmt):
    by = {t["raw_desc"]: t for t in stmt["transactions"]}
    assert by["Lidl sagt Danke"]["channel"] == "card"
    assert by["Lidl sagt Danke"]["bank_cat"] == "Lebensmittel"
    assert by["WEG Musterstrasse 1"]["channel"] == "Belastungen"
    assert by["WEG Musterstrasse 1"]["counter_iban"] == "DE00500502010000000002"
    # 银行没给类别的转账，bank_cat 必须是 None 而不是空字符串 —— 下游按 None 走兜底
    assert by["An Savings"]["bank_cat"] is None


def test_每笔都有起息日(stmt):
    """`Wertstellung` 是一笔交易的结束标记。缺了说明状态机没断干净，
    后续补充行会被错误地挂到这一笔上。"""
    assert all("value_date" in t for t in stmt["transactions"])


def test_id对同一份对账单稳定(stmt):
    """id 稳定 = 重导幂等。不稳定的话每次重导都新增一批，人工标注全散架。"""
    again = parse_text(_STATEMENT, fallback_name="statement-2026-03")
    assert [t["id"] for t in again["transactions"]] == [t["id"] for t in stmt["transactions"]]


def test_月份取自账期而不是文件名(stmt):
    """上传来的文件可以叫任何名字。认文件名会把 3 月的账算到别的月头上。"""
    assert stmt["month"] == "2026-03"
    misnamed = parse_text(_STATEMENT, fallback_name="下载 (3).pdf")
    assert misnamed["month"] == "2026-03"
    assert all(t["month"] == "2026-03" for t in misnamed["transactions"])


def test_换个文件名不改变id(stmt):
    """id 里含月份；月份若跟着文件名走，同一份对账单换个名字就会生成另一批
    id —— 重导变成「新增一份重复数据」，而不是更新。"""
    renamed = parse_text(_STATEMENT, fallback_name="whatever")
    assert [t["id"] for t in renamed["transactions"]] == [t["id"] for t in stmt["transactions"]]


def test_同日同额同商家的两笔不会撞id():
    """3 月有两笔同日、同商家的 GO ASIA —— 只按「日期+金额+商家」算哈希会
    把它们合成一条，少算一笔钱。序号进哈希正是为了这个。"""
    ids = set()
    for seq in (1, 2):
        t = {"month": "2026-03", "section": "main", "seq": seq,
             "date": "2026-03-01", "amount": -1.79, "raw_desc": "GO ASIA DEUTSCHLAND"}
        import hashlib
        ids.add(hashlib.sha1(
            f"{t['month']}|{t['section']}|{t['seq']}|{t['date']}|{t['amount']}|{t['raw_desc']}".encode()
        ).hexdigest()[:16])
    assert len(ids) == 2


# ── 分类规则 ──


def _mk(desc, amount=-10.0, bank_cat=None, channel="card"):
    return classify({"raw_desc": desc, "amount": amount,
                     "bank_cat": bank_cat, "channel": channel})


@pytest.mark.parametrize("desc,l1,l2", [
    ("Lidl sagt Danke",           "餐饮", "超市生鲜"),
    ("Erika Vermieter",            "住房", "房租"),
    ("Vodafone GmbH",             "通信数码", "手机资费"),
    ("DM DROGERIE SAGT DANKE",    "健康医疗", "药妆日化"),
    ("An Savings",                    "非消费", "储蓄划转"),
    ("Überweisungsgebühr",        "金融税费", "手续费"),
])
def test_商家规则命中(desc, l1, l2):
    t = _mk(desc)
    assert (t["l1"], t["l2"]) == (l1, l2)


def test_没命中商家规则时退回银行自带类别():
    t = _mk("Irgendein Neuer Laden", bank_cat="Bars & Restaurants")
    assert (t["l1"], t["l2"]) == ("餐饮", "餐厅")


def test_都没命中就是未分类而不是猜():
    t = _mk("Völlig Unbekannt XY")
    assert t["l1"] == "未分类"


@pytest.mark.parametrize("desc,want", [
    ("AMAZON PRIM* XA2X44ES5", "Amazon Prime"),
    ("AMAZON PRIM* MQ7OT07N5", "Amazon Prime"),
])
def test_随机订单号必须剥掉否则规则沉淀不下来(desc, want):
    assert _mk(desc)["merchant"] == want


def test_未命中规则的商家也要剥订单号():
    # 这类商家没有专门规则，靠通用剥离维持同名 —— 否则每月长出一个新商家
    assert _mk("SumUp *Neuer Laden A1B2C3D4")["merchant"] == "SumUp *Neuer Laden"


def test_划转与收入不算支出():
    assert _mk("An Savings", amount=-500.0)["kind"] == "transfer"
    assert _mk("Acme Analytics", amount=2608.45)["kind"] == "income"


def test_正数的普通消费是退款应当冲减原类别而非算收入():
    t = _mk("Spotify", amount=12.99)
    assert t["kind"] == "refund"
    assert t["l1"] == "通信数码"


def test_取现归到非消费():
    """取现不是消费，是现金转移 —— 现金花在哪银行看不见，混进支出会凭空多算。"""
    t = _mk("FRANKFURTER SPARKASSE", amount=-100.0, bank_cat="Geldautomat")
    assert (t["kind"], t["l1"], t["l2"]) == ("transfer", "非消费", "现金提取")


def test_固定支出被标出来():
    assert _mk("Erika Vermieter")["fixed"] is True
    assert _mk("Lidl sagt Danke")["fixed"] is False


def test_用户字段初始为空等着人工填():
    t = _mk("Lidl sagt Danke")
    assert t["tags"] == [] and t["note"] == ""
    assert t["reviewed"] is False and t["auto"] is True
