"""账单业务层 —— 导入编排、规则套用、汇总聚合。

## 导入的三道闸

1. **同文件去重**：按 sha256 认文件，重复上传直接返回上次结果，不重复解析
2. **余额勾稽**：期初 + 本期合计 = 期末，不平**拒绝入库**（`force=True` 可越过，
   但会在批次里留下 `ok=False` 的记录）
3. **人工字段保护**：交给 `repositories.import_transactions` 的 `$setOnInsert`

## 聚合为什么放服务层而不是前端

前端已经证明「同一个数字在页面多处出现必须相等」很容易出错（实测踩过两次：
固定/可变漏减退款、类别占比没跟着口径走）。把口径收敛到一个 `summary()`
里算一次、各视图只负责画，是唯一能保证一致的做法。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from app.core.logging import get_logger
from app.features.finance.models import Statement, Transaction
from app.features.finance.repo import finance_repo
from app.features.finance import parse as finance_parse
from app.features.finance.categories import seed_payload

logger = get_logger(__name__)


def ensure_seeded() -> None:
    finance_repo.seed_categories(seed_payload())


# ── 导入 ──


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# 解析层用 None 表示「这笔没有这个字段」（转账没有银行类别、刷卡没有对方
# IBAN），模型侧统一成空字符串：前端渲染 `null` 会显示成 "null"，而"没有"
# 和"空"在这里语义相同，不值得为它多一种状态。
_OPTIONAL_STR = ("value_date", "bank_cat", "channel", "counter_iban", "memo")


def _str_fields(t: dict) -> dict:
    return {k: (t.get(k) or "") for k in _OPTIONAL_STR}


def import_pdf(path: Path, file_name: str = "", force: bool = False) -> dict:
    """导入一份对账单 PDF。

    返回 {ok, month, statement, imported, skipped, reason}。
    `ok=False` 时**没有任何数据入库** —— 勾稽不平意味着解析漏了交易，
    宁可不导也不让一笔钱无声消失。
    """
    raw = path.read_bytes()
    sha = _sha256(raw)
    name = file_name or path.name

    existing = finance_repo.get_statement_by_hash(sha)
    if existing and not force:
        return {
            "ok": existing.get("ok", False),
            "skipped": True,
            "reason": "同一个文件已经导入过（按内容哈希判定）",
            "month": existing.get("month"),
            "statement": existing,
        }

    stmt = finance_parse.parse_and_classify(path)
    if not stmt["ok"] and not force:
        bad = [r for r in stmt["reconcile"] if not r["ok"]]
        return {
            "ok": False,
            "skipped": True,
            "reason": "余额勾稽不平，解析必定漏了交易，已拒绝入库",
            "month": stmt["month"],
            "reconcile": stmt["reconcile"],
            "detail": bad,
        }

    # 库里已有的商家规则优先于代码里的默认规则 —— 用户教过的，机器要记得
    rules = finance_repo.rules_map()
    txs = []
    for t in stmt["transactions"]:
        r = rules.get(t["merchant"])
        if r:
            t = {**t, "l1": r["l1"], "l2": r["l2"],
                 "kind": r.get("kind", t["kind"]), "fixed": bool(r.get("fixed"))}
        txs.append(Transaction(**{**t, "statement_id": sha, **_str_fields(t)}))

    res = finance_repo.import_transactions(txs)
    saved = finance_repo.save_statement(Statement(
        month=stmt["month"], file_name=name, file_sha256=sha,
        period_start=(stmt["period"] or ("", ""))[0],
        period_end=(stmt["period"] or ("", ""))[1],
        tx_count=len(txs), reconcile=stmt["reconcile"], ok=stmt["ok"],
    ))
    logger.info("账单导入 %s：%s 笔（新增 %s）", stmt["month"], len(txs), res["inserted"])
    return {"ok": True, "skipped": False, "month": stmt["month"],
            "statement": saved, "imported": res}


def import_dir(src: Path) -> list[dict]:
    """批量导入一个目录下的 statement-*.pdf。首次搭建/恢复备份时用。"""
    out = []
    for p in sorted(src.glob("*.pdf")):
        try:
            out.append(import_pdf(p))
        except Exception as e:                       # noqa: BLE001
            logger.exception("导入失败 %s", p)
            out.append({"ok": False, "skipped": True, "reason": str(e), "file": p.name})
    return out


# ── 逐条编辑 ──


def patch_transaction(tx_id: str, patch: dict, learn_rule: bool = False) -> dict:
    """改一笔交易；可选把这次归类沉淀成商家规则并套用到同商家未确认的条目。"""
    doc = finance_repo.patch_tx(tx_id, patch)
    if not doc:
        return {"ok": False, "reason": "交易不存在"}
    applied = 0
    if learn_rule and doc.get("merchant"):
        values = {k: doc[k] for k in ("l1", "l2", "kind", "fixed")}
        finance_repo.upsert_rule(doc["merchant"], values)
        applied = finance_repo.apply_rule_to_untouched(doc["merchant"], values, skip_id=tx_id)
    return {"ok": True, "tx": doc, "applied": applied}


# ── 汇总聚合 ──
#
# 口径统一在这里算一次。`only_var=True` 时滤掉固定支出 —— 房租占 73% 且每月
# 雷打不动，含它时每张分布图第一眼都是「房租最大」，那是无法据以行动的信息。

def _net(rows: list[dict]) -> float:
    """支出减退款。退款冲减原类别，不是收入。"""
    exp = sum(abs(t["amount"]) for t in rows if t["kind"] == "expense")
    ref = sum(abs(t["amount"]) for t in rows if t["kind"] == "refund")
    return round(exp - ref, 2)


def summary(months: list[str], only_var: bool = True) -> dict:
    all_rows = [t for t in finance_repo.list_tx(months=months) if not t.get("excluded")]
    spend = [t for t in all_rows if t["kind"] in ("expense", "refund")]
    trf = [t for t in all_rows if t["kind"] == "transfer"]

    total = _net(spend)
    fixed = _net([t for t in spend if t.get("fixed")])
    var = round(total - fixed, 2)
    scoped = [t for t in spend if not (only_var and t.get("fixed"))]
    scoped_total = _net(scoped)

    n = max(len(months), 1)
    by_month = {m: _net([t for t in spend if t["month"] == m]) for m in months}
    var_by_month = {m: _net([t for t in spend if t["month"] == m and not t.get("fixed")])
                    for m in months}

    # 类别（含二级下钻）
    cats: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "subs": defaultdict(float)})
    for t in scoped:
        sign = -1 if t["kind"] == "refund" else 1
        v = sign * abs(t["amount"])
        cats[t["l1"]]["total"] += v
        cats[t["l1"]]["subs"][t.get("l2") or "（未细分）"] += v
    cat_rows = sorted(
        ({"l1": k, "total": round(o["total"], 2),
          "subs": sorted(({"l2": s, "total": round(x, 2)} for s, x in o["subs"].items()),
                         key=lambda d: -d["total"])}
         for k, o in cats.items() if o["total"] > 0),
        key=lambda d: -d["total"],
    )

    # 每月 × 大类，供堆叠柱用
    stack: dict[str, dict[str, float]] = {m: defaultdict(float) for m in months}
    for t in scoped:
        sign = -1 if t["kind"] == "refund" else 1
        stack[t["month"]][t["l1"]] += sign * abs(t["amount"])
    stack_rows = [
        {"month": m,
         "parts": {k: round(v, 2) for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if v > 0},
         "total": round(sum(v for v in d.values() if v > 0), 2)}
        for m, d in ((m, stack[m]) for m in months)
    ]

    # 商家
    merch: dict[str, dict] = defaultdict(lambda: {"n": 0, "total": 0.0, "l1": "", "fixed": False})
    for t in scoped:
        o = merch[t.get("merchant") or t["raw_desc"]]
        o["n"] += 1 if t["kind"] == "expense" else 0
        o["total"] += (-1 if t["kind"] == "refund" else 1) * abs(t["amount"])
        o["l1"] = t["l1"]
        o["fixed"] = bool(t.get("fixed"))
    merchants = sorted(
        ({"merchant": k, **{kk: (round(vv, 2) if isinstance(vv, float) else vv)
                            for kk, vv in o.items()}}
         for k, o in merch.items() if o["total"] > 0),
        key=lambda d: -d["total"],
    )[:15]

    # 日历（ISO 日期为键，跨月不撞车）
    daily: dict[str, float] = defaultdict(float)
    for t in scoped:
        daily[t["date"]] += (-1 if t["kind"] == "refund" else 1) * abs(t["amount"])

    # 标签
    tagm: dict[str, dict] = defaultdict(lambda: {"n": 0, "total": 0.0})
    for t in all_rows:
        for tag in t.get("tags") or []:
            tagm[tag]["n"] += 1
            tagm[tag]["total"] += (-1 if t["kind"] == "refund" else 1) * abs(t["amount"])
    tags = sorted(({"tag": k, "n": o["n"], "total": round(o["total"], 2)}
                   for k, o in tagm.items()), key=lambda d: -d["total"])

    # 非消费：**只统计主账户那条腿**。一笔「主账户 → Savings Space」在 PDF 里是
    # 两条记录，两条都算净额恒为 0，会显示成「一分钱没存」。
    nonc: dict[str, dict] = defaultdict(lambda: {"out": 0.0, "in": 0.0, "n": 0})
    for t in trf:
        if t.get("section") != "main":
            continue
        o = nonc[t.get("l2") or "其他"]
        o["n"] += 1
        if t["amount"] < 0:
            o["out"] += -t["amount"]
        else:
            o["in"] += t["amount"]
    nonconsume = [{"kind": k, "out": round(o["out"], 2), "in": round(o["in"], 2),
                   "net": round(o["in"] - o["out"], 2), "n": o["n"]}
                  for k, o in sorted(nonc.items(), key=lambda kv: -(kv[1]["out"] + kv[1]["in"]))]

    fixed_names = sorted({t.get("merchant", "") for t in spend if t.get("fixed")} - {""})
    exp_n = sum(1 for t in spend if t["kind"] == "expense")

    return {
        "months": months,
        "only_var": only_var,
        "totals": {
            "total": total, "fixed": fixed, "var": var,
            "scoped": scoped_total,
            "per_month": round(total / n, 2),
            "var_per_month": round(var / n, 2),
            "fixed_per_month": round(fixed / n, 2),
            "count": exp_n,
            "avg_tx": round(total / exp_n, 2) if exp_n else 0,
        },
        "by_month": by_month,
        "var_by_month": var_by_month,
        "categories": cat_rows,
        "stack": stack_rows,
        "merchants": merchants,
        "daily": {k: round(v, 2) for k, v in daily.items()},
        "tags": tags,
        "nonconsume": nonconsume,
        "fixed_names": fixed_names,
    }


def month_progress() -> list[dict]:
    """每月的整理进度，给月份选择器用。"""
    rows = finance_repo.list_tx()
    by: dict[str, dict] = defaultdict(lambda: {"total": 0, "reviewed": 0, "spend": []})
    for t in rows:
        o = by[t["month"]]
        o["total"] += 1
        o["reviewed"] += 1 if t.get("reviewed") else 0
        if not t.get("excluded") and t["kind"] in ("expense", "refund"):
            o["spend"].append(t)
    return [
        {"month": m, "total": o["total"], "reviewed": o["reviewed"],
         "expense": _net(o["spend"])}
        for m, o in sorted(by.items())
    ]
