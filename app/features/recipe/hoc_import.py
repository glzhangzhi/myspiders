"""CookLikeHOC 仓库的 Markdown 菜谱解析与导入。

源仓库：https://github.com/Gar-b-age/CookLikeHOC

**格式是不齐的**（340 个文件里实测）：
- 章节标题有 `## 配料` / `## 原料` / `## 原料：` / `## 步骤:` / 带尾随 Tab 等变体
- 85 个文件没有「配料」节（多为配料表本身或饮品，只有「已知成分」「营养成分」）
- 步骤行有 `- 1. xxx` / `1. xxx` / `- xxx` 三种写法
- 配料行会带括注（供应商）和内部链接 `[小炒肉调料](/配料/小炒肉调料.md)`

故解析器的原则是：**能抽多少抽多少，抽不出就如实标记**，把 raw_text 一并存下来，
由用户决定要不要丢给 DeepSeek 转换器补齐（见 recipe_convert.py）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from app.core.logging import get_logger
from app.features.recipe.models import Ingredient, Recipe, RecipeStep
from app.features.recipe.tags import derive_tags, make_slug

logger = get_logger("recipe_import")

# 章节标题：`## 配料`、`## 原料：`、`## 步骤:`、带尾随空白等
_HEADING = re.compile(r"^#{2,3}\s*(?P<name>[^\s:：]+)\s*[:：]?\s*$")
_TITLE = re.compile(r"^#\s+(?P<title>.+?)\s*$")

_INGREDIENT_SECTIONS = {"配料", "原料", "材料", "已知成分", "用料"}
_STEP_SECTIONS = {"步骤", "做法", "制作", "流程"}

# 列表项：`- 1. xxx` / `1. xxx` / `- xxx` / `* xxx`
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s*)?(?:(?P<num>\d+)\s*[.、)]\s*)?(?P<text>.+?)\s*$")

# Markdown 链接 `[名字](/路径.md)` → 只保留名字
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# 图片行 `![xx](yy)`
_MD_IMAGE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")

# 配料行里的用量：开头的 `30g` / `2 勺` / 结尾的 `（少许）`
_AMOUNT_PREFIX = re.compile(
    r"^(?P<amount>[\d./]+\s*(?:g|kg|ml|L|毫升|克|千克|斤|两|个|只|条|根|片|勺|匙|杯|包|袋|颗|瓣|把|块|张)?)\s*(?P<rest>.+)$"
)


def _clean_line(line: str) -> str:
    return _MD_LINK.sub(r"\1", line).strip()


def _split_parens(text: str) -> tuple[list[str], str]:
    """按括号深度切分：返回 (顶层括注列表, 去掉括注后的正文)。

    深度计数而不是正则，才能正确处理嵌套括注（见调用处注释）。
    中英文括号混用也很常见，两种都认。
    """
    opens, closes = "（(", "）)"
    notes: list[str] = []
    kept: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in opens:
            if depth == 0:
                buf = []
            else:
                buf.append(ch)
            depth += 1
        elif ch in closes and depth > 0:
            depth -= 1
            if depth == 0:
                notes.append("".join(buf).strip())
            else:
                buf.append(ch)
        elif depth > 0:
            buf.append(ch)
        else:
            kept.append(ch)
    if depth > 0:                      # 括号没闭合，当正文处理免得吞内容
        kept.append("".join(buf))
    return [n for n in notes if n], "".join(kept).strip()


def parse_ingredient_line(line: str) -> Optional[Ingredient]:
    """把一行配料文本拆成 name / amount / note。

    括注（供应商、可选说明）统一进 note —— 做饭时它不重要，但删掉又可惜。
    """
    text = _clean_line(line)
    if not text:
        return None

    # 抽出所有括注，正文留干净的主料名。
    # 必须按深度扫描而非正则：源数据里有嵌套括号
    # 「小炒肉调料（聚慧（重庆）、烟台欣和）」——
    # 非贪婪正则会在第一个 "）" 处收尾，把「、烟台欣和）」留在主料名里。
    notes, text = _split_parens(text)
    if not text:
        # 整行都是括注（罕见），把括注本身当名字，免得丢内容
        text = "、".join(notes)
        notes = []

    amount = ""
    m = _AMOUNT_PREFIX.match(text)
    # 只有带单位或纯数字打头才算用量，避免把「五花肉」的「五」当数量
    if m and re.match(r"^[\d./]", m.group("amount")):
        amount = m.group("amount").strip()
        text = m.group("rest").strip()

    if not text:
        return None
    return Ingredient(name=text, amount=amount, note="；".join(n.strip() for n in notes if n.strip()))


def parse_markdown(content: str, category: str = "", fallback_title: str = "") -> dict:
    """解析一份 CookLikeHOC 风格的 Markdown。

    Returns:
        {"title", "ingredients": [Ingredient], "steps": [RecipeStep], "extra": {节名: [行]}}
    """
    title = fallback_title
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None

    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _MD_IMAGE.match(line):
            continue

        m_title = _TITLE.match(line)
        if m_title and not title:
            title = _clean_line(m_title.group("title"))
            continue
        if m_title:
            continue

        m_head = _HEADING.match(line)
        if m_head:
            current = m_head.group("name").strip()
            sections.setdefault(current, [])
            continue

        if current is not None:
            sections[current].append(line)

    ingredients: list[Ingredient] = []
    steps: list[RecipeStep] = []
    extra: dict[str, list[str]] = {}

    for name, lines in sections.items():
        items = []
        for line in lines:
            m = _LIST_ITEM.match(line)
            if m and m.group("text"):
                items.append(_clean_line(m.group("text")))

        if name in _INGREDIENT_SECTIONS:
            for item in items:
                ing = parse_ingredient_line(item)
                if ing:
                    ingredients.append(ing)
        elif name in _STEP_SECTIONS:
            for item in items:
                text = item.strip()
                if text:
                    steps.append(RecipeStep(order=len(steps) + 1, text=text))
        else:
            extra[name] = items

    return {"title": title, "ingredients": ingredients, "steps": steps, "extra": extra}


def build_recipe(
    content: str,
    category: str = "",
    fallback_title: str = "",
    source: str = "cooklikehoc",
    source_url: str = "",
) -> Recipe:
    """Markdown → Recipe（含标签派生）。"""
    parsed = parse_markdown(content, category=category, fallback_title=fallback_title)
    title = parsed["title"] or fallback_title or "未命名"
    ingredients: list[Ingredient] = parsed["ingredients"]
    steps: list[RecipeStep] = parsed["steps"]

    tips_parts = []
    for name, items in parsed["extra"].items():
        if items:
            tips_parts.append(f"【{name}】" + "；".join(items))

    tags = derive_tags(
        title=title,
        category=category,
        ingredients=[f"{i.amount}{i.name}{i.note}" for i in ingredients],
        steps=[s.text for s in steps],
    )

    # 没抽出步骤 = 规则解析没吃透这份文档，标记出来让前端提示「用 AI 转换」
    status = "parsed" if steps else "needs_convert"

    return Recipe(
        slug=make_slug(title, source),
        title=title,
        category=category,
        ingredients=ingredients,
        steps=steps,
        method_tags=tags["method"],
        flavor_tags=tags["flavor"],
        ingredient_tags=tags["ingredient"],
        tips="\n".join(tips_parts),
        source=source,
        source_url=source_url,
        raw_text=content,
        parse_status=status,
    )


# 非菜谱目录 / 文件：仓库自带的说明与 Docker 支持
_SKIP_DIRS = {"docs", "docker_support", "images", ".git", "node_modules"}
_SKIP_FILES = {"README.md", "readme.md"}


def iter_recipe_files(root: Path) -> Iterable[tuple[Path, str]]:
    """遍历仓库，产出 (文件路径, 分类目录名)。"""
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            continue
        if path.name in _SKIP_FILES:
            continue
        category = rel.parts[0] if len(rel.parts) > 1 else ""
        yield path, category


def import_from_directory(
    root: str | Path,
    source: str = "cooklikehoc",
    url_prefix: str = "https://github.com/Gar-b-age/CookLikeHOC/blob/main",
    overwrite: bool = False,
) -> dict:
    """把一个本地 CookLikeHOC 克隆导入库中。

    overwrite=False 时跳过已存在的 slug（默认），避免覆盖用户后来编辑过的内容。
    """
    from app.features.recipe.repo import recipe_repo

    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {root}")

    imported = skipped = failed = 0
    needs_convert: list[str] = []

    for path, category in iter_recipe_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("读取失败 %s: %s", path, exc)
            failed += 1
            continue

        try:
            rel = path.relative_to(root).as_posix()
            recipe = build_recipe(
                content,
                category=category,
                fallback_title=path.stem,
                source=source,
                source_url=f"{url_prefix}/{rel}",
            )
            if not overwrite and recipe_repo.exists(recipe.slug):
                skipped += 1
                continue
            recipe_repo.upsert(recipe)
            imported += 1
            if recipe.parse_status == "needs_convert":
                needs_convert.append(recipe.title)
        except Exception as exc:
            logger.warning("解析失败 %s: %s", path, exc)
            failed += 1

    logger.info(
        "CookLikeHOC 导入完成: 新增 %d, 跳过 %d, 失败 %d, 待转换 %d",
        imported, skipped, failed, len(needs_convert),
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "needs_convert": needs_convert,
    }
