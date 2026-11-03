"""标签词表持久化 —— 做法 / 口味 / 主材 / 分类 四组，全部可增删改。

原先词表是 `recipe_tags.py` 里的硬编码常量，用户加不了自己的标签。
现在改成：**代码里的常量只是"出厂默认值"**，首次启动播种进 Mongo，
之后以库为准。派生逻辑（关键词匹配）仍在代码里 —— 新加的标签没有关键词，
属于纯人工标签，这与 AV 模块「内容标签纯人工」的约定一致。

集合 `recipe_tag_vocab`，一个 group 一条文档：
    {"group": "method", "tags": ["炒", "蒸", ...]}
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.clients.mongo import get_collection
from app.core.settings import settings

GROUPS = ("method", "flavor", "ingredient", "category")

GROUP_LABELS = {
    "method": "做法",
    "flavor": "口味",
    "ingredient": "主材",
    "category": "分类",
}

# 菜谱文档里与每个 group 对应的字段名（改标签时要同步更新存量菜谱）
GROUP_FIELDS = {
    "method": "method_tags",
    "flavor": "flavor_tags",
    "ingredient": "ingredient_tags",
    "category": "category",       # 分类是单值字符串，不是数组
}


class TagVocabRepository:
    def __init__(self):
        self.collection = get_collection("recipe_tag_vocab", settings.mongo_default_db)
        self.collection.create_index("group", unique=True)

    def _seed_if_empty(self) -> None:
        """首次使用时用代码里的默认词表播种。

        用 $setOnInsert：已有记录（用户改过的）绝不覆盖。
        """
        from app.features.recipe.tags import (
            DEFAULT_CATEGORIES,
            FLAVOR_TAGS,
            INGREDIENT_TAGS,
            METHOD_TAGS,
        )

        defaults = {
            "method": METHOD_TAGS,
            "flavor": FLAVOR_TAGS,
            "ingredient": INGREDIENT_TAGS,
            "category": DEFAULT_CATEGORIES,
        }
        for group, tags in defaults.items():
            self.collection.update_one(
                {"group": group},
                {"$setOnInsert": {
                    "group": group,
                    "tags": list(tags),
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )

    def all_groups(self) -> dict[str, list[str]]:
        self._seed_if_empty()
        out: dict[str, list[str]] = {}
        for doc in self.collection.find({}, {"_id": 0}):
            out[doc["group"]] = doc.get("tags") or []
        return {g: out.get(g, []) for g in GROUPS}

    def get(self, group: str) -> list[str]:
        return self.all_groups().get(group, [])

    def add(self, group: str, tag: str) -> list[str]:
        """新增标签（幂等，追加到末尾保持用户看到的顺序稳定）。"""
        tag = (tag or "").strip()
        if group not in GROUPS:
            raise ValueError(f"未知标签组: {group}")
        if not tag:
            raise ValueError("标签不能为空")
        self._seed_if_empty()
        self.collection.update_one({"group": group}, {"$addToSet": {"tags": tag}})
        return self.get(group)

    def remove(self, group: str, tag: str) -> list[str]:
        """删除标签，并从所有引用它的菜谱上摘掉。

        不做"还有 N 道菜在用，拒绝删除"的保护：这是单人自用的库，
        用户说删就是想删干净，留着孤儿标签反而更碍事。
        """
        if group not in GROUPS:
            raise ValueError(f"未知标签组: {group}")
        self._seed_if_empty()
        self.collection.update_one({"group": group}, {"$pull": {"tags": tag}})

        recipes = get_collection("recipes", settings.mongo_default_db)
        field = GROUP_FIELDS[group]
        if group == "category":
            recipes.update_many({"category": tag}, {"$set": {"category": ""}})
        else:
            recipes.update_many({field: tag}, {"$pull": {field: tag}})
        return self.get(group)

    def rename(self, group: str, old: str, new: str) -> list[str]:
        """重命名标签，并同步改写所有引用。"""
        new = (new or "").strip()
        if group not in GROUPS:
            raise ValueError(f"未知标签组: {group}")
        if not new:
            raise ValueError("新名称不能为空")
        if old == new:
            return self.get(group)

        self._seed_if_empty()
        tags = self.get(group)
        if old not in tags:
            raise ValueError(f"标签不存在: {old}")

        # 改名撞上已有标签时相当于合并 —— 先去掉旧的，再确保新的在列表里
        self.collection.update_one({"group": group}, {"$pull": {"tags": old}})
        self.collection.update_one({"group": group}, {"$addToSet": {"tags": new}})

        recipes = get_collection("recipes", settings.mongo_default_db)
        field = GROUP_FIELDS[group]
        if group == "category":
            recipes.update_many({"category": old}, {"$set": {"category": new}})
        else:
            # 先记下受影响的 slug，再 $pull 旧的、$addToSet 新的。
            # 不能用数组定位符一步改名：菜谱可能同时带着新旧两个标签，
            # 那样会留下重复项。分两步 + $addToSet 天然幂等。
            affected = [
                d["slug"] for d in recipes.find({field: old}, {"slug": 1, "_id": 0})
            ]
            if affected:
                recipes.update_many({"slug": {"$in": affected}}, {"$pull": {field: old}})
                recipes.update_many({"slug": {"$in": affected}}, {"$addToSet": {field: new}})
        return self.get(group)

    def replace(self, group: str, tags: list[str]) -> list[str]:
        """整组覆盖（管理面板里排序/批量编辑后保存）。"""
        if group not in GROUPS:
            raise ValueError(f"未知标签组: {group}")
        clean: list[str] = []
        for t in tags or []:
            t = str(t).strip()
            if t and t not in clean:
                clean.append(t)
        self._seed_if_empty()
        self.collection.update_one(
            {"group": group},
            {"$set": {"tags": clean, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return clean

    def usage_counts(self, group: str) -> dict[str, int]:
        """每个标签被多少道菜使用（管理面板显示，删之前心里有数）。"""
        recipes = get_collection("recipes", settings.mongo_default_db)
        field = GROUP_FIELDS[group]
        if group == "category":
            pipeline = [{"$group": {"_id": "$category", "count": {"$sum": 1}}}]
        else:
            pipeline = [
                {"$unwind": f"${field}"},
                {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
            ]
        return {d["_id"]: d["count"] for d in recipes.aggregate(pipeline) if d["_id"]}


tag_vocab_repo = TagVocabRepository()
