"""账单 / 支出持久化。

四个集合：
- `fin_transactions`    交易主体，主键 id（内容哈希）
- `fin_statements`      导入批次，主键 file_sha256
- `fin_merchant_rules`  商家 → 分类规则，主键 merchant
- `fin_categories`      分类词表，主键 l1

## 重导 PDF 绝不能冲掉人工结果

`_USER_OWNED` 是这个模块最要紧的一行。用户花两小时逐条确认完一个月，
一次「重新导入」就该原样保留 —— 导入路径只 `$set` 机器字段，人工字段走
`$setOnInsert`（新交易才给默认值，已有的一律不碰）。

这条不是假想风险：菜谱模块就踩过同构的坑（`prev_snapshot` 被同一次 upsert
抹掉），那次是「留底→upsert 全量」的顺序问题，这里是「重导→覆盖」。
根因相同：**用 model_dump() 全量 $set**。所以这里从不全量 $set。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pymongo import ASCENDING, DESCENDING, UpdateOne

from app.clients.mongo import get_collection
from app.core.settings import settings
from app.features.finance.models import Statement, Transaction

# 人工拥有的字段 —— 重新导入 PDF 时**只在新建时写入**，已存在的一律保留。
# 加字段时务必同步这里，否则那个字段会在下次重导时被机器值覆盖。
_USER_OWNED = (
    "merchant", "kind", "l1", "l2", "fixed", "excluded",
    "tags", "note", "reviewed", "auto",
)

# 机器拥有的字段 —— 重导时以 PDF 为准覆盖。
# 注意 amount/date 也在此列：它们可被人工改（改错了要能纠），但 PDF 是
# 事实来源，重导意味着「以银行为准重新对一遍」，此时应当覆盖回去。
_MACHINE = (
    "month", "section", "seq", "date", "value_date", "amount",
    "raw_desc", "memo", "channel", "bank_cat", "counter_iban", "statement_id",
)


class FinanceRepository:
    def __init__(self):
        db = settings.mongo_default_db
        self.tx = get_collection("fin_transactions", db)
        self.stmts = get_collection("fin_statements", db)
        self.rules = get_collection("fin_merchant_rules", db)
        self.cats = get_collection("fin_categories", db)

        self.tx.create_index("id", unique=True)
        self.tx.create_index([("month", ASCENDING), ("date", ASCENDING), ("seq", ASCENDING)])
        self.tx.create_index("merchant")
        self.tx.create_index("l1")
        self.tx.create_index("tags")
        self.tx.create_index("reviewed")
        self.stmts.create_index("file_sha256", unique=True)
        self.stmts.create_index("month")
        self.rules.create_index("merchant", unique=True)
        self.cats.create_index("l1", unique=True)
        self.closed = get_collection("fin_closed_months", db)
        self.closed.create_index("month", unique=True)

    # ── 月度封账 ──
    #
    # 单独一个集合而不是塞进 fin_statements：封账是**人对某个月的判断**，
    # 与「导入过哪些文件」是两回事。同一个月可能导入过两次（补发的对账单），
    # 挂在批次上就要处理「哪一批说了算」这种没有意义的问题。

    def closed_months(self) -> list[str]:
        return sorted(d["month"] for d in self.closed.find({}, {"_id": 0, "month": 1}))

    def set_closed(self, month: str, closed: bool) -> None:
        if closed:
            self.closed.update_one(
                {"month": month},
                {"$setOnInsert": {"month": month, "closed_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        else:
            self.closed.delete_one({"month": month})

    # ── 交易 ──

    def get_tx(self, tx_id: str) -> Optional[dict]:
        return self.tx.find_one({"id": tx_id}, {"_id": 0})

    def list_tx(
        self,
        months: Optional[list[str]] = None,
        reviewed: Optional[bool] = None,
        kind: Optional[str] = None,
        search: Optional[str] = None,
        section: Optional[str] = None,
    ) -> list[dict]:
        q: dict = {}
        if months:
            q["month"] = {"$in": months}
        if reviewed is not None:
            q["reviewed"] = reviewed
        if kind:
            q["kind"] = kind
        if section:
            q["section"] = section
        if search:
            # 必须转义 —— 商家名里带 * 和 ( 很常见（`PAYPAL *SHOPAPOTHEK`、
            # `REWE (907) Frankfu`），未转义的 "(" 是非法正则，整个搜索 500。
            esc = re.escape(search.strip())
            q["$or"] = [
                {"merchant": {"$regex": esc, "$options": "i"}},
                {"raw_desc": {"$regex": esc, "$options": "i"}},
                {"memo": {"$regex": esc, "$options": "i"}},
                {"note": {"$regex": esc, "$options": "i"}},
                {"tags": {"$regex": esc, "$options": "i"}},
            ]
        return list(
            self.tx.find(q, {"_id": 0}).sort([("date", ASCENDING), ("seq", ASCENDING)])
        )

    def months(self) -> list[str]:
        return sorted(self.tx.distinct("month"))

    def import_transactions(self, txs: list[Transaction]) -> dict:
        """批量导入。**人工字段只在新建时写入，已有的一律保留。**

        用 bulk_write 而不是逐条 update_one：一次导入 60~90 笔，逐条往返
        在远程 Mongo 上要几百毫秒，合并成一次则是个位数。
        """
        if not txs:
            return {"inserted": 0, "updated": 0}

        now = datetime.now(timezone.utc)
        ops = []
        for t in txs:
            doc = t.model_dump()
            machine = {k: doc[k] for k in _MACHINE if k in doc}
            machine["updated_at"] = now
            user = {k: doc[k] for k in _USER_OWNED if k in doc}
            user["created_at"] = doc.get("created_at", now)
            ops.append(UpdateOne(
                {"id": t.id},
                {"$set": machine, "$setOnInsert": {"id": t.id, **user}},
                upsert=True,
            ))
        res = self.tx.bulk_write(ops, ordered=False)
        return {
            "inserted": res.upserted_count,
            "updated": res.modified_count,
            "total": len(txs),
        }

    def patch_tx(self, tx_id: str, patch: dict) -> Optional[dict]:
        """逐条编辑。只写传进来的字段 —— None 表示"不改"，不是"清空"。"""
        clean = {k: v for k, v in patch.items() if v is not None}
        if not clean:
            return self.get_tx(tx_id)
        clean["updated_at"] = datetime.now(timezone.utc)
        # 人工动过之后就不再是机器分类了，后续「按规则批量跟进」会跳过它
        if any(k in clean for k in ("l1", "l2", "kind", "merchant", "fixed")):
            clean["auto"] = False
        self.tx.update_one({"id": tx_id}, {"$set": clean})
        return self.get_tx(tx_id)

    def apply_rule_to_untouched(self, merchant: str, values: dict, skip_id: str = "") -> int:
        """把规则套用到同商家「机器分类且未确认」的交易上。

        **绝不动用户已经手工改过的条目**（auto=False 或 reviewed=True）——
        否则你精心改过的某一笔会被后来的一次规则学习悄悄改回去。
        """
        q = {
            "merchant": merchant,
            "auto": True,
            "reviewed": False,
            "id": {"$ne": skip_id},
        }
        upd = {k: v for k, v in values.items() if k in ("l1", "l2", "kind", "fixed")}
        if not upd:
            return 0
        upd["updated_at"] = datetime.now(timezone.utc)
        return self.tx.update_many(q, {"$set": upd}).modified_count

    def rename_category(self, old: str, new: str, sub: Optional[str] = None) -> int:
        """改名同步改写所有引用的交易 —— 不留孤儿数据。"""
        if sub is None:
            return self.tx.update_many({"l1": old}, {"$set": {"l1": new}}).modified_count
        return self.tx.update_many(
            {"l1": old, "l2": sub[0]}, {"$set": {"l2": sub[1]}}
        ).modified_count

    def reset_category(self, l1: str) -> int:
        """删除大类：受影响的交易退回未分类，并**取消已确认状态** ——
        它们需要重新归类，不该继续以「已确认」的面目混在统计里。"""
        return self.tx.update_many(
            {"l1": l1},
            {"$set": {"l1": "未分类", "l2": "", "reviewed": False, "auto": True}},
        ).modified_count

    def clear_sub(self, l1: str, sub: str) -> int:
        return self.tx.update_many({"l1": l1, "l2": sub}, {"$set": {"l2": ""}}).modified_count

    # ── 导入批次 ──

    def get_statement_by_hash(self, sha: str) -> Optional[dict]:
        return self.stmts.find_one({"file_sha256": sha}, {"_id": 0})

    def save_statement(self, stmt: Statement) -> dict:
        doc = stmt.model_dump()
        self.stmts.update_one(
            {"file_sha256": stmt.file_sha256}, {"$set": doc}, upsert=True
        )
        return self.get_statement_by_hash(stmt.file_sha256)

    def list_statements(self) -> list[dict]:
        return list(self.stmts.find({}, {"_id": 0}).sort("month", DESCENDING))

    def delete_statement(self, sha: str) -> dict:
        """删除一次导入连同它的交易。人工标注一并消失 —— 调用方须二次确认。"""
        stmt = self.get_statement_by_hash(sha)
        if not stmt:
            return {"deleted": 0, "tx": 0}
        n = self.tx.delete_many({"statement_id": sha}).deleted_count
        self.stmts.delete_one({"file_sha256": sha})
        return {"deleted": 1, "tx": n}

    # ── 商家规则 ──

    def list_rules(self) -> list[dict]:
        return list(self.rules.find({}, {"_id": 0}).sort("merchant", ASCENDING))

    def upsert_rule(self, merchant: str, values: dict) -> None:
        self.rules.update_one(
            {"merchant": merchant},
            {
                "$set": {
                    "l1": values.get("l1", ""),
                    "l2": values.get("l2", ""),
                    "kind": values.get("kind", "expense"),
                    "fixed": bool(values.get("fixed")),
                    "updated_at": datetime.now(timezone.utc),
                },
                "$inc": {"hits": 1},
            },
            upsert=True,
        )

    def delete_rule(self, merchant: str) -> int:
        return self.rules.delete_one({"merchant": merchant}).deleted_count

    def rules_map(self) -> dict[str, dict]:
        return {r["merchant"]: r for r in self.list_rules()}

    # ── 分类词表 ──

    def seed_categories(self, defaults: list[dict]) -> None:
        """用出厂默认值播种。$setOnInsert —— 用户改过的绝不覆盖。"""
        for i, d in enumerate(defaults):
            self.cats.update_one(
                {"l1": d["l1"]},
                {"$setOnInsert": {
                    "l1": d["l1"],
                    "subs": list(d.get("subs", [])),
                    "order": d.get("order", i),
                    "color": d.get("color", ""),
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )

    def list_categories(self) -> list[dict]:
        return list(self.cats.find({}, {"_id": 0}).sort("order", ASCENDING))

    def add_category(self, l1: str, color: str = "") -> None:
        n = self.cats.count_documents({})
        self.cats.update_one(
            {"l1": l1},
            {"$setOnInsert": {
                "l1": l1, "subs": [], "order": n, "color": color,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    def rename_category_vocab(self, old: str, new: str) -> None:
        doc = self.cats.find_one({"l1": old})
        if not doc:
            return
        self.cats.update_one({"l1": old}, {"$set": {"l1": new}})
        self.rules.update_many({"l1": old}, {"$set": {"l1": new}})

    def delete_category_vocab(self, l1: str) -> None:
        self.cats.delete_one({"l1": l1})
        self.rules.delete_many({"l1": l1})

    def set_subs(self, l1: str, subs: list[str]) -> None:
        self.cats.update_one({"l1": l1}, {"$set": {"subs": subs}})

    def rename_sub(self, l1: str, old: str, new: str) -> None:
        doc = self.cats.find_one({"l1": l1})
        if not doc:
            return
        subs = [new if s == old else s for s in doc.get("subs", [])]
        self.cats.update_one({"l1": l1}, {"$set": {"subs": subs}})
        self.rules.update_many({"l1": l1, "l2": old}, {"$set": {"l2": new}})

    def delete_sub(self, l1: str, sub: str) -> None:
        doc = self.cats.find_one({"l1": l1})
        if not doc:
            return
        subs = [s for s in doc.get("subs", []) if s != sub]
        self.cats.update_one({"l1": l1}, {"$set": {"subs": subs}})
        self.rules.update_many({"l1": l1, "l2": sub}, {"$set": {"l2": ""}})


finance_repo = FinanceRepository()
