"""菜谱 / 排餐持久化。

两个集合：
- `recipes`     —— 主键 slug
- `meal_plans`  —— 推送列表，单条文档 key="current"
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from app.clients.mongo import get_collection
from app.core.settings import settings
from app.features.recipe.models import Recipe

# 人工维护、代码永不覆盖的字段 —— 重新导入 / 重新转换时必须原样保留，
# 否则用户加的自定义标签和收藏会被一次「重新导入」全部抹掉。
#
# last_pushed_at 也在此列：upsert 写的是完整模型、其中它默认 None ——
# 不保护的话一次 AI 重转换就会把「推送过」的标记抹掉。
_USER_OWNED = ("custom_tags", "favorite", "last_pushed_at")

# 「我自己的」菜谱 = 不是从公开菜谱库批量导入的（手动录入 / AI 转换的都算）。
# 用排除法而非列举法：将来加新的录入入口（比如网页剪藏）自动算「我的」，
# 而新增导入源时必须想起来加进这里 —— 后者是一次显式的改动，前者容易忘。
IMPORTED_SOURCES = ("howtocook", "cooklikehoc")


class RecipeRepository:
    SORT_FIELDS = {
        "created_at": "created_at",
        "title": "title",
        "last_pushed_at": "last_pushed_at",
        "total_minutes": "total_minutes",
    }

    def __init__(self):
        self.collection = get_collection("recipes", settings.mongo_default_db)
        self.collection.create_index("slug", unique=True)
        self.collection.create_index("category")
        self.collection.create_index("method_tags")
        self.collection.create_index("flavor_tags")
        self.collection.create_index("ingredient_tags")
        self.collection.create_index("custom_tags")
        self.collection.create_index("title")
        self._migrate()

    def _migrate(self) -> None:
        """一次性迁移，幂等（条件里带 $exists，跑过一次后什么都匹配不到）。

        - 「做过几次」改成「是否推送过」：cooked_count 是推送时 +1 的，本来就
          不是真的做过几次；last_cooked_at 正好就是最近一次推送的时刻，改名即可。
        - 「还原」功能已移除：prev_snapshot 摘掉。
        """
        self.collection.update_many(
            {"last_cooked_at": {"$exists": True}},
            {"$rename": {"last_cooked_at": "last_pushed_at"}},
        )
        self.collection.update_many(
            {"$or": [{"cooked_count": {"$exists": True}}, {"prev_snapshot": {"$exists": True}}]},
            {"$unset": {"cooked_count": "", "prev_snapshot": ""}},
        )

    # ── 读 ──

    def get(self, slug: str) -> Optional[dict]:
        return self.collection.find_one({"slug": slug}, {"_id": 0})

    def get_many(self, slugs: list[str]) -> list[dict]:
        """按传入顺序返回（Mongo 的 $in 不保证顺序，而排餐是有序的）。"""
        docs = {
            d["slug"]: d
            for d in self.collection.find({"slug": {"$in": slugs}}, {"_id": 0})
        }
        return [docs[s] for s in slugs if s in docs]

    def exists(self, slug: str) -> bool:
        return self.collection.count_documents({"slug": slug}, limit=1) > 0

    @staticmethod
    def _build_query(
        methods: Optional[list[str]] = None,
        flavors: Optional[list[str]] = None,
        ingredients: Optional[list[str]] = None,
        custom: Optional[list[str]] = None,
        category: Optional[str] = None,
        search: Optional[str] = None,
        favorite: Optional[bool] = None,
        source: Optional[str] = None,
        mine: Optional[bool] = None,
    ) -> dict:
        query: dict = {}
        # 组内 AND（选了「炒」+「蒸」= 既炒又蒸的菜）。组间自然也是 AND。
        if methods:
            query["method_tags"] = {"$all": methods}
        if flavors:
            query["flavor_tags"] = {"$all": flavors}
        if ingredients:
            query["ingredient_tags"] = {"$all": ingredients}
        if custom:
            query["custom_tags"] = {"$all": custom}
        if category:
            query["category"] = category
        if favorite is True:
            query["favorite"] = True
        if source:
            query["source"] = source
        elif mine is True:
            query["source"] = {"$nin": list(IMPORTED_SOURCES)}
        if search:
            # 必须转义：菜名带括号很常见（「农家小炒肉（玉耳版本）」），
            # 未转义的 "(" 是非法正则 → 整个搜索 500。
            escaped = re.escape(search.strip())
            query["$or"] = [
                {"title": {"$regex": escaped, "$options": "i"}},
                {"ingredients.name": {"$regex": escaped, "$options": "i"}},
            ]
        return query

    def list_all(
        self,
        skip: int = 0,
        limit: int = 50,
        sort_by: str = "created_at",
        sort_order: int = -1,
        **filters,
    ) -> list[dict]:
        query = self._build_query(**filters)
        sort_field = self.SORT_FIELDS.get(sort_by, "created_at")
        return list(
            self.collection.find(query, {"_id": 0})
            .sort([(sort_field, sort_order), ("slug", 1)])
            .skip(skip)
            .limit(limit)
        )

    def count(self, **filters) -> int:
        return self.collection.count_documents(self._build_query(**filters))

    def facet_counts(self, **filters) -> dict:
        """当前筛选下每个标签的共现数量（侧栏计数，避免点出零结果）。"""
        base = self._build_query(**filters)

        def _counts(field: str) -> dict:
            return {
                d["_id"]: d["count"]
                for d in self.collection.aggregate([
                    {"$match": base},
                    {"$unwind": f"${field}"},
                    {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
                ])
                if d["_id"]
            }

        categories = {
            d["_id"]: d["count"]
            for d in self.collection.aggregate([
                {"$match": base},
                {"$group": {"_id": "$category", "count": {"$sum": 1}}},
            ])
            if d["_id"]
        }
        # 「我的」计数：不受「只看我的」本身影响，否则勾上之后计数等于总数、
        # 取消之前看不出它会筛掉多少。
        mine_base = self._build_query(**{**filters, "mine": None})
        mine_base["source"] = {"$nin": list(IMPORTED_SOURCES)}
        if filters.get("source"):
            mine_base["source"] = {"$in": [filters["source"]], "$nin": list(IMPORTED_SOURCES)}
        return {
            "mine": self.collection.count_documents(mine_base),
            "method": _counts("method_tags"),
            "flavor": _counts("flavor_tags"),
            "ingredient": _counts("ingredient_tags"),
            "custom": _counts("custom_tags"),
            "category": categories,
        }

    def stats(self) -> dict:
        total = self.collection.count_documents({})
        return {
            "total": total,
            "favorite": self.collection.count_documents({"favorite": True}),
            "converted": self.collection.count_documents({"parse_status": "converted"}),
            "needs_review": self.collection.count_documents({"parse_status": "parsed", "steps": []}),
        }

    def random_pick(self, n: int = 3, **filters) -> list[dict]:
        """随机抽 n 道（「今天吃什么」选择困难时用）。"""
        return list(self.collection.aggregate([
            {"$match": self._build_query(**filters)},
            {"$sample": {"size": max(1, n)}},
            {"$project": {"_id": 0}},
        ]))

    # ── 写 ──

    def upsert(self, recipe: Recipe, preserve_user_fields: bool = True) -> dict:
        """写入或覆盖。

        preserve_user_fields=True 时保留库中已有的人工字段（自定义标签、收藏、
        做过次数）—— 重新导入 / AI 重转换不该清掉用户自己积累的东西。
        """
        doc = recipe.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc)

        if preserve_user_fields:
            old = self.get(recipe.slug)
            if old:
                # 只有**传入值为空**时才沿用旧值。不能写成
                # `if old[field] not in (None, [], 0)` —— Python 里 False == 0，
                # 那个判断会把 favorite=False 当成"旧值为空"而跳过，语义含混；
                # 更要紧的是它让调用方无法主动清空一个字段。
                for field in _USER_OWNED:
                    incoming = doc.get(field)
                    if incoming in (None, "", [], 0, False) and field in old:
                        doc[field] = old[field]
                doc["created_at"] = old.get("created_at", doc["created_at"])

        self.collection.update_one({"slug": recipe.slug}, {"$set": doc}, upsert=True)
        return self.get(recipe.slug)

    def update_fields(self, slug: str, fields: dict) -> Optional[dict]:
        fields = {**fields, "updated_at": datetime.now(timezone.utc)}
        self.collection.update_one({"slug": slug}, {"$set": fields})
        return self.get(slug)

    def delete(self, slug: str) -> bool:
        return self.collection.delete_one({"slug": slug}).deleted_count > 0

    def mark_pushed(self, slugs: list[str]) -> None:
        """推送到 Kindle 成功后调用：记录最近一次推送时间（有值 = 推送过）。"""
        self.collection.update_many(
            {"slug": {"$in": slugs}},
            {"$set": {"last_pushed_at": datetime.now(timezone.utc)}},
        )

    def toggle_favorite(self, slug: str, favorite: bool) -> Optional[dict]:
        return self.update_fields(slug, {"favorite": bool(favorite)})


class MealPlanRepository:
    """推送列表（原「当前菜单」）—— 全库**单条**文档（固定 key）。

    原先按日期分条 + 历史列表，实际用不上：做饭是"现在要做这几道菜"，
    不是日程管理。改成单条后，前端刷新拿到的就是用户最后一次编辑的状态。
    """

    _KEY = "current"

    def __init__(self):
        self.collection = get_collection("meal_plans", settings.mongo_default_db)
        self.collection.create_index("key", unique=True)

    def get_current(self) -> Optional[dict]:
        return self.collection.find_one({"key": self._KEY}, {"_id": 0})

    def save_current(self, slugs: list[str], note: str = "") -> dict:
        now = datetime.now(timezone.utc)
        self.collection.update_one(
            {"key": self._KEY},
            {
                "$set": {"slugs": list(slugs), "note": note, "updated_at": now},
                "$setOnInsert": {"key": self._KEY, "created_at": now},
            },
            upsert=True,
        )
        return self.get_current()

    def mark_pushed(self) -> None:
        self.collection.update_one(
            {"key": self._KEY},
            {"$set": {"pushed_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    def clear(self) -> None:
        self.save_current([], "")


recipe_repo = RecipeRepository()
meal_plan_repo = MealPlanRepository()
