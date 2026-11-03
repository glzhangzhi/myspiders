"""N26 德语对账单 PDF → 结构化交易。

**纯函数层**：不碰 Mongo、不依赖 FastAPI，只做「解析 + 规则预分类」。
入库与业务编排在 `finance_service.py`，持久化在 `repositories/finance.py`。
因为不碰 DB，这里的逻辑可以被 `tests/test_finance_parse.py` 直接覆盖 ——
它属于本项目约定的「少数高风险纯函数」那一类。

依赖外部命令 `pdftotext`（poppler-utils）。

## 版式（pdftotext -layout 之后）

每笔交易恒为：
    <描述>            <记账日 dd.mm.yyyy>   <±金额>€
随后若干补充行，直到 `Wertstellung <起息日>` 为止：
    Mastercard • <银行类别>     刷卡消费，带银行自己的德语类别
    Lastschriften / Belastungen / Gutschriften   直接借记 / 转账 / 入账
    IBAN: ... • BIC: ...
    <用途备注>（可多行）

## 为什么按余额勾稽

「期初 + 本期合计 = 期末」是解析没漏没错的唯一硬证明。PDF 版式一旦变动
（N26 改模板、新增交易类型），正则很可能静默少抓一笔而不报错 —— 勾稽不平
就该拒绝入库，而不是让一笔钱无声消失在统计里。实测 6 个月 × 2 段全部平。

## 主账户与 Space 是两本账

`Space Kontoauszug` 之后的交易属于储蓄 Space（假设叫 Savings），与主账户里的
`An Savings` / `Von Savings` 是同一笔钱的两条腿。两边都计入支出就是双重计数，
故 Space 段单独标 section，默认不进支出统计。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

# ── 版式正则 ──

RE_TX = re.compile(r"^(?P<desc>.+?)\s{2,}(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<amt>[+-][\d.]+,\d{2})€\s*$")
RE_BAL = re.compile(r"^Dein (alter|neuer) Kontostand\s+([+-][\d.]+,\d{2})€")
RE_WERT = re.compile(r"^Wertstellung (\d{2}\.\d{2}\.\d{4})")
RE_CARD = re.compile(r"^(Mastercard|Maestro|Visa)\s*•\s*(.+)$")
RE_IBAN = re.compile(r"^IBAN:\s*(\S+)(?:\s*•\s*BIC:\s*(\S+))?")
RE_PERIOD = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}) bis (\d{2})\.(\d{2})\.(\d{4})")
# 页眉页脚 —— 每页重复出现，必须先滤掉，否则会被当成交易的补充行混进备注
#
# 页脚三行按**版式**认，不按内容认（户名 / 地址 / 本人 IBAN 因人而异，写死就是
# 把个人信息写进代码）：
#   <户名>                         Erstellt am
#   <地址>                         <开具日期 dd.mm.yyyy>
#   IBAN: <本人> • BIC: <…>        Nr. <mm/yyyy>
# 交易行同样是「文字 + 两个以上空格 + 日期」，但后面一定还跟着金额，
# 而页脚第 2 行以日期**结尾**，二者不会混淆（RE_TX 先于此判定不成立：
# 本正则在 RE_TX 之前匹配，所以靠的是 `$` 锚定的「日期结尾」）。
RE_NOISE = re.compile(
    r"^(Kontoauszug( Nr\.)?|Space Kontoauszug.*|Spaces Zusammenfassung.*"
    r"|Beschreibung\s+Verbuchungsdatum\s+Betrag"
    r"|.+\s{2,}Erstellt am"
    r"|.+\s{2,}\d{2}\.\d{2}\.\d{4}"
    r"|IBAN: \S+ • BIC: \S+\s{2,}Nr\. \d{2}/\d{4}"
    r"|Erstellt am|\d{2}\.\d{2}\.\d{4}"
    r"|Nr\. \d{2}/\d{4}|\d+ / \d+|Space: .*|Anmerkung|Dein N26 Team)$"
)
TYPE_WORDS = {"Belastungen", "Lastschriften", "Gutschriften", "Überweisungen"}


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _iso(d: str) -> str:
    dd, mm, yyyy = d.split(".")
    return f"{yyyy}-{mm}-{dd}"


def pdf_to_text(path: Path) -> str:
    """跑 pdftotext。单独抽出来，测试可以只替换这一层而不去 mock subprocess。"""
    return subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, text=True, check=True,
    ).stdout


def parse_pdf(path: Path) -> dict:
    """解析单个对账单，返回 {month, period, balances, transactions}。"""
    return parse_text(pdf_to_text(path), fallback_name=path.stem)


def parse_text(txt: str, fallback_name: str = "") -> dict:
    """解析已抽出的文本。月份优先取 PDF 里的账期，文件名只作兜底 ——
    上传来的文件可能叫任何名字，认文件名会把 3 月的账算到 8 月头上。"""

    section = "main"
    balances: dict[str, dict] = {}
    txs: list[dict] = []
    cur: dict | None = None
    period = None
    seq = 0

    for raw in txt.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        if s.startswith("Space Kontoauszug"):
            section = "space"
        elif s.startswith("Spaces Zusammenfassung"):
            section = "space"      # 汇总段只有余额，沿用 space
        if period is None:
            m = RE_PERIOD.match(s)
            if m:
                period = (_iso(f"{m[1]}.{m[2]}.{m[3]}"), _iso(f"{m[4]}.{m[5]}.{m[6]}"))
        b = RE_BAL.match(s)
        if b:
            balances.setdefault(section, {})["opening" if b[1] == "alter" else "closing"] = _num(b[2])
            continue
        if RE_NOISE.match(s):
            continue

        m = RE_TX.match(line)
        if m:
            seq += 1
            cur = {
                "seq": seq,
                "section": section,
                "raw_desc": re.sub(r"\s+", " ", m["desc"]).strip(),
                "date": _iso(m["date"]),
                "amount": _num(m["amt"]),
                "bank_cat": None,
                "channel": None,      # card / Lastschriften / Belastungen / Gutschriften / MoneyBeam
                "counter_iban": None,
                "memo": [],
            }
            txs.append(cur)
            continue
        if cur is None:
            continue

        w = RE_WERT.match(s)
        if w:
            cur["value_date"] = _iso(w[1])
            cur = None            # 一笔到此结束
            continue
        c = RE_CARD.match(s)
        if c:
            cur["channel"] = "card"
            cur["bank_cat"] = c[2].strip()
            continue
        if s in ("Mastercard", "Maestro", "Visa"):
            cur["channel"] = "card"
            continue
        i = RE_IBAN.match(s)
        if i:
            cur["counter_iban"] = i[1]
            continue
        if s in TYPE_WORDS:
            cur["channel"] = s
            continue
        if s == "MoneyBeam":
            cur["channel"] = "MoneyBeam"
            continue
        cur["memo"].append(s)

    if period:
        month = period[0][:7]                      # 账期起始日 2026-03-01 → 2026-03
    else:
        m = re.search(r"(\d{4})-(\d{2})", fallback_name)
        month = f"{m[1]}-{m[2]}" if m else ""
    for t in txs:
        t["memo"] = " ".join(t["memo"])
        t["month"] = month
        t["id"] = hashlib.sha1(
            f"{month}|{t['section']}|{t['seq']}|{t['date']}|{t['amount']}|{t['raw_desc']}".encode()
        ).hexdigest()[:16]
    return {"month": month, "period": period, "balances": balances, "transactions": txs}


def reconcile(stmt: dict) -> list[dict]:
    """期初 + 本期合计 = 期末。不平就是解析漏了，调用方应当拒绝入库。"""
    out = []
    for sec, bal in stmt["balances"].items():
        total = round(sum(t["amount"] for t in stmt["transactions"] if t["section"] == sec), 2)
        o, c = bal.get("opening"), bal.get("closing")
        diff = None if o is None or c is None else round(o + total - c, 2)
        out.append({
            "section": sec, "opening": o, "closing": c, "sum": total,
            "diff": diff, "ok": diff == 0,
            "count": sum(1 for t in stmt["transactions"] if t["section"] == sec),
        })
    return out


# ── 商家归一 + 默认分类规则 ──
#
# 规则 = (正则, 归一后的商家名, 大类, 二级, 是否固定支出)。**顺序敏感**，先匹配先生效。
# 商家名里的随机订单号必须剥掉（`AMAZON PRIM* XA2X44ES5` 每月都不一样），
# 否则 91 个商家会膨胀成上百个、规则永远沉淀不下来。
KIND_TRANSFER, KIND_INCOME, KIND_EXPENSE, KIND_REFUND = "transfer", "income", "expense", "refund"

# 涉及个人的规则（自己的 Space 名、本人跨境汇款、房东、雇主、亲友……）**不写在代码里**，
# 放在 .env 的 FINANCE_PERSONAL_RULES（JSON 数组，每项同下面的五元组），排在通用规则前面。
# 另外用户在页面上逐条确认过的归类会沉淀进 fin_merchant_rules，那一层优先级最高。
RULES: list[tuple[str, str, str, str, bool]] = [
    # —— 非消费：自己两个口袋之间搬钱 ——
    (r"^(An|Von) Main Account$",         "主账户",         "非消费", "储蓄划转", False),
    # —— 住房 ——
    (r"^KlickEnergie",                   "KlickEnergie",   "住房",   "水电燃气", True),
    (r"^Rundfunk ARD",                   "广播电视费",     "住房",   "物业杂费", True),
    (r"^Abfluss-AS",                     "Abfluss-AS",     "住房",   "家居维修", False),
    # —— 通信数码 ——
    (r"^Vodafone",                       "Vodafone",       "通信数码", "手机资费", True),
    (r"^AMAZON PRIM",                    "Amazon Prime",   "通信数码", "订阅服务", True),
    (r"^SPOTIFY|^Spotify",               "Spotify",        "通信数码", "订阅服务", True),
    (r"^HP INSTANT INK",                 "HP Instant Ink", "通信数码", "订阅服务", True),
    (r"^APPLE STORE",                    "Apple",          "通信数码", "订阅服务", True),
    (r"^DeepSeek",                       "DeepSeek",       "通信数码", "云服务软件", False),
    (r"^BYTEHOSTING",                    "Bytehosting",    "通信数码", "云服务软件", True),
    # —— 交通 ——
    (r"^DB Vertrieb",                    "DB 铁路",        "交通",   "长途火车", True),
    (r"^Seilbahn",                       "吕德斯海姆缆车", "交通",   "其他交通", False),
    # —— 餐饮：超市 ——
    (r"^Lidl",                           "Lidl",           "餐饮",   "超市生鲜", False),
    (r"^ALDI",                           "ALDI",           "餐饮",   "超市生鲜", False),
    (r"^REWE",                           "REWE",           "餐饮",   "超市生鲜", False),
    (r"^Penny",                          "Penny",          "餐饮",   "超市生鲜", False),
    (r"^Tegut",                          "Tegut",          "餐饮",   "超市生鲜", False),
    (r"^Globus",                         "Globus",         "餐饮",   "超市生鲜", False),
    (r"^Galeria Markth",                 "Galeria 市场",   "餐饮",   "超市生鲜", False),
    (r"^GO ASIA",                        "GO ASIA 亚超",   "餐饮",   "超市生鲜", False),
    (r"^Orient Master",                  "Orient Master",  "餐饮",   "超市生鲜", False),
    (r"^Nanapan",                        "Nanapan",        "餐饮",   "超市生鲜", False),
    (r"^Fischmarkt",                     "Mare Azurro 鱼市", "餐饮", "超市生鲜", False),
    (r"^Nespresso",                      "Nespresso",      "餐饮",   "咖啡饮品", False),
    # —— 餐饮：烘焙 / 餐厅 / 快餐 ——
    (r"^Frisch Balkan",                  "Balkan 面包房",  "餐饮",   "面包烘焙", False),
    (r"^Baeckerei Doerzenbach|^Schaefer Dein Baecker|^Ditsch", "面包房", "餐饮", "面包烘焙", False),
    (r"^Tandoori",                       "Tandoori",       "餐饮",   "餐厅",     False),
    (r"^Melis Kebap",                    "Melis Kebap",    "餐饮",   "餐厅",     False),
    (r"^Mangetsu",                       "Mangetsu",       "餐饮",   "餐厅",     False),
    (r"^Hankook",                        "Hankook",        "餐饮",   "餐厅",     False),
    (r"^SHIINSAN",                       "Shiinsan",       "餐饮",   "餐厅",     False),
    (r"^SumUp \*Xin Chao",               "Xin Chao",       "餐饮",   "餐厅",     False),
    (r"^Chicking",                       "Chicking",       "餐饮",   "快餐外带", False),
    (r"^BURGER KING",                    "Burger King",    "餐饮",   "快餐外带", False),
    (r"^MCDONALDS",                      "McDonald's",     "餐饮",   "快餐外带", False),
    (r"^KFC",                            "KFC",            "餐饮",   "快餐外带", False),
    (r"^TGTG",                           "Too Good To Go", "餐饮",   "快餐外带", False),
    (r"^LS Pallina",                     "Pallina Gelato", "餐饮",   "快餐外带", False),
    (r"^Coco Bubble",                    "Coco 奶茶",      "餐饮",   "咖啡饮品", False),
    (r"^KIOSKIE",                        "Kioskie 咖啡",   "餐饮",   "咖啡饮品", False),
    (r"^4everKiosk",                     "4everKiosk",     "餐饮",   "零食烟酒", False),
    (r"^Buergerspital Weingut",          "Bürgerspital 酒庄", "餐饮", "零食烟酒", False),
    # —— 娱乐休闲 ——
    (r"^KIOSK AM PLATZ",                 "Kiosk am Platz", "娱乐休闲", "小酌酒水", False),
    (r"^LEGO Store",                     "LEGO",           "娱乐休闲", "玩具收藏", False),
    # —— 健康 ——
    (r"^DM DROGERIE",                    "dm",             "健康医疗", "药妆日化", False),
    (r"^Rossmann",                       "Rossmann",       "健康医疗", "药妆日化", False),
    (r"^MUeLLER",                        "Müller",         "健康医疗", "药妆日化", False),
    (r"^PAYPAL \*SHOPAPOTHEK",           "Shop Apotheke",  "健康医疗", "药品",     False),
    # —— 购物 ——
    (r"^Uniqlo",                         "Uniqlo",         "购物",   "服饰鞋包", False),
    (r"^COS ",                           "COS",            "购物",   "服饰鞋包", False),
    (r"^PAYPAL \*ZALANDO",               "Zalando",        "购物",   "服饰鞋包", False),
    (r"^SP UCON ACROBATICS",             "Ucon Acrobatics", "购物",  "服饰鞋包", False),
    (r"^Woolworth",                      "Woolworth",      "购物",   "家居日用", False),
    (r"^TEDI",                           "TEDi",           "购物",   "家居日用", False),
    (r"^PFA PFLANZEN",                   "Pflanzen für Alle", "购物", "家居日用", False),
    (r"^SATURN",                         "Saturn",         "购物",   "电器",     False),
    (r"^(AMAZON|AMZN|WWW\.AMAZON|Amazon\.de)", "Amazon",   "购物",   "其他购物", False),
    (r"^eBay",                           "eBay",           "购物",   "其他购物", False),
    (r"^E&M Shop|^MIXX",                 None,             "购物",   "其他购物", False),
    # —— 个人护理 / 职业 ——
    (r"^One Hair Salon",                 "One Hair Salon", "个人护理", "理发",   False),
    (r"^BUNDESAMT FUER JUSTIZ",          "联邦司法局",     "教育职业", "证件规费", False),
    # —— 金融 ——
    (r"^Überweisungsgebühr",             "跨境汇款手续费", "金融税费", "手续费", False),
    (r"^N26$",                           "N26 取现费",     "金融税费", "手续费", False),
]

# 银行自带的德语类别 → 我的分类（仅当上面的商家规则没命中时兜底）
BANK_CAT_FALLBACK = {
    "Lebensmittel":            ("餐饮", "超市生鲜"),
    "Bars & Restaurants":      ("餐饮", "餐厅"),
    "Gesundheit & Drogerien":  ("健康医疗", "药妆日化"),
    "Freizeit":                ("娱乐休闲", "其他休闲"),
    "Shopping":                ("购物", "其他购物"),
    "Medien & Telekom":        ("通信数码", "订阅服务"),
    "Berufsausgaben":          ("教育职业", "职业开销"),
    "Transport":               ("交通", "其他交通"),
    "Geldautomat":             ("非消费", "现金提取"),
    "Sonstiges":               (None, None),
}

def personal_rules() -> list[tuple[str, str, str, str, bool]]:
    """读 .env 的 FINANCE_PERSONAL_RULES；格式错误时忽略（只记日志），不让导入失败。"""
    import json
    import logging

    from app.core.settings import settings

    raw = (getattr(settings, "finance_personal_rules", "") or "").strip()
    if not raw:
        return []
    try:
        return [(str(p), n, str(a), str(b), bool(f)) for p, n, a, b, f in json.loads(raw)]
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("FINANCE_PERSONAL_RULES 不是合法的 JSON 五元组数组，已忽略")
        return []


def _compiled() -> list:
    return [(re.compile(p), name, l1, l2, fx) for p, name, l1, l2, fx in (*personal_rules(), *RULES)]
# 尾部随机订单号：`AMAZON PRIM* XA2X44ES5` / `eBay O*14-14842-78349` / `Spotify P4088258A6`
_RE_ORDERNO = re.compile(r"[\s*]+[A-Z0-9][A-Z0-9*\-]{5,}$")


def classify(tx: dict) -> dict:
    """给一笔交易补上 merchant / kind / l1 / l2 / fixed。纯规则，用户可在页面逐条推翻。"""
    desc = tx["raw_desc"]
    merchant, l1, l2, fixed = None, None, None, False
    for rx, name, c1, c2, fx in _compiled():
        if rx.search(desc):
            merchant, l1, l2, fixed = (name or desc), c1, c2, fx
            break
    if merchant is None:
        merchant = _RE_ORDERNO.sub("", desc).strip() or desc
        if tx.get("bank_cat") in BANK_CAT_FALLBACK:
            l1, l2 = BANK_CAT_FALLBACK[tx["bank_cat"]]
    if l1 is None:
        l1, l2 = "未分类", ""

    if l1 == "非消费":
        kind = KIND_TRANSFER
    elif l1 == "收入":
        kind = KIND_INCOME
    elif tx["amount"] > 0:
        kind = KIND_REFUND        # 正数又不是收入/划转 → 退款，应当冲减原类别
    else:
        kind = KIND_EXPENSE

    tx.update({
        "merchant": merchant, "kind": kind, "l1": l1, "l2": l2 or "",
        "fixed": fixed, "tags": [], "note": "", "reviewed": False, "auto": True,
    })
    return tx


def parse_and_classify(path: Path) -> dict:
    """解析 + 勾稽 + 预分类，一步到位。服务层的唯一入口。

    返回 {month, period, balances, reconcile, transactions, ok}。
    `ok=False` 表示余额勾稽不平 —— **调用方必须拒绝入库**，别让一笔钱
    无声消失在统计里。
    """
    stmt = parse_pdf(path)
    stmt["reconcile"] = reconcile(stmt)
    stmt["ok"] = all(r["ok"] for r in stmt["reconcile"]) and bool(stmt["reconcile"])
    for t in stmt["transactions"]:
        classify(t)
    return stmt
