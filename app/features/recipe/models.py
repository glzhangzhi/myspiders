"""菜谱数据模型。

设计取舍：**菜谱的唯一键是 slug（来源+标题派生）**，不是标题本身 ——
同名菜在不同来源（CookLikeHOC / 手动录入 / 网上粘贴）会有不同做法，
它们是不同的菜谱，不该互相覆盖。

`ingredients` 与 `steps` 是结构化的，因为 Kindle 渲染需要按「配料清单 /
步骤」分区排版；而 `raw_text` 保留原始文本，供转换失败时人工回看。
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class Ingredient(BaseModel):
    """一味配料。

    量词不拆成数值+单位：源数据里 "30g 米粉"、"少许盐"、"两三片姜" 混杂，
    强行结构化只会丢信息。做饭时人眼读的是整串文本，保持原样最有用。
    """

    name: str                                  # 主料名，如「五花肉片」
    amount: str = ""                           # 用量原文，如「200g」「少许」，可空
    note: str = ""                             # 括注，如「河南双汇」「可选」


class RecipeStep(BaseModel):
    """一个步骤。order 从 1 起，渲染到 Kindle 时直接当序号。"""

    order: int
    text: str


class Recipe(BaseModel):
    """一道菜，对应 MongoDB `recipes` 集合。"""

    slug: str                                  # 主键，见 services/recipe_tags.make_slug
    title: str
    category: str = ""                         # 源分类：炒菜 / 蒸菜 / 汤…（CookLikeHOC 的目录名）

    ingredients: list[Ingredient] = Field(default_factory=list)
    steps: list[RecipeStep] = Field(default_factory=list)

    # ── 筛选维度（见 services/recipe_tags.py 的三类定义）──
    method_tags: list[str] = Field(default_factory=list)      # 做法：炒 / 蒸 / 炖…
    flavor_tags: list[str] = Field(default_factory=list)      # 口味：香辣 / 清淡 / 酸甜…
    ingredient_tags: list[str] = Field(default_factory=list)  # 主材：猪肉 / 鸡蛋 / 豆腐…
    custom_tags: list[str] = Field(default_factory=list)      # 人工自由标签，代码永不改动

    # ── 辅助信息 ──
    servings: str = ""                         # 「2 人份」，自由文本
    total_minutes: Optional[int] = None        # 预估耗时（分钟），DeepSeek 给或人工填
    tips: str = ""                             # 窍门 / 注意事项

    source: str = "manual"                     # cooklikehoc / manual / deepseek
    source_url: str = ""
    raw_text: str = ""                         # 原始文本，转换失败时的兜底

    # 「解析质量」标记：parsed=规则解析，converted=DeepSeek 转换，manual=人工录入。
    # 规则解析可能漏信息，前端据此提示「建议用 AI 转换一下」。
    parse_status: str = "parsed"

    # 最近一次推送到 Kindle 的时间；有值 = 推送过（Kindle 上有这道菜的文件）。
    # 取代了原先的 cooked_count：那个数是推送时 +1，从来不是真的「做过几次」。
    last_pushed_at: Optional[datetime] = None
    favorite: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CurrentMenu(BaseModel):
    """推送列表，对应 MongoDB `meal_plans` 集合里**唯一**一条文档。

    不按日期分条：做饭是"现在要做这几道菜"，不是日程管理。推送时每道菜
    一个文件、按菜名覆盖，库里不做版本管理。
    """

    key: str = "current"                             # 固定主键
    slugs: list[str] = Field(default_factory=list)   # 选中的菜，有序
    note: str = ""
    pushed_at: Optional[datetime] = None             # 最近一次推送时间
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
