"""HowToCook（Anduin2017/HowToCook）菜谱导入。

源仓库：https://github.com/Anduin2017/HowToCook —— 程序员写给自己做饭用的，
**面向家庭厨房**，与 CookLikeHOC 的商用后厨口径正相反。

格式高度统一（实测 370 份 100% 命中四段），解析器可以写得很确定：

    # <菜名>的做法
    <简介，含难度/耗时描述>
    预估烹饪难度：★★★
    预估卡路里：997 大卡
    ## 必备原料和工具   → 配料名（无用量）
    ## 计算             → 用量公式（「西红柿 = 1 个 * 份数」）
    ## 操作             → 步骤，66/370 带 ### 小节分组
    ## 附加内容         → 贴士

关键取舍：**「必备原料」与「计算」要合并**。前者只有名字、后者只有用量，
分开存会得到"一堆没有量的料 + 一堆没有主语的量"。按名字对齐后合成一条。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from app.core.logging import get_logger
from app.features.recipe.models import Ingredient, Recipe, RecipeStep
from app.features.recipe.tags import derive_tags, make_slug

logger = get_logger("recipe_htc")

# 目录名 → 本系统分类。
#
# 源仓库按**菜品类型**分目录（荤/素/水产/汤…），这与"做法"是两个维度。
# 早期版本把 meat/vegetable/aquatic 一律映射成「炒菜」，结果 370 道里
# 201 道挤在同一个分类下，筛选器等于失效。现在照搬源仓库的类型语义 ——
# 做法维度由 method_tags 承担（derive_tags 从步骤里推），两者互不干扰。
CATEGORY_MAP = {
    "meat_dish": "荤菜",
    "vegetable_dish": "素菜",
    "aquatic": "水产",
    "soup": "汤",
    "staple": "主食",
    "breakfast": "早餐",
    "dessert": "甜点",
    "drink": "饮品",
    "condiment": "配料",
    "semi-finished": "半成品",
}

_TITLE = re.compile(r"^#\s+(?P<name>.+?)(?:的做法)?\s*$")
_DIFFICULTY = re.compile(r"预估烹饪难度：\s*(?P<stars>★+)")
_CALORIE = re.compile(r"预估卡路里：\s*(?P<kcal>[\d.]+)")
_MD_IMAGE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
# 行内图片：`盛盘 ![成品](./x.jpg)` —— 只跳过独占一行的不够，实测 9 处步骤
# 把图片标记带进了正文，推到 Kindle 上就是一行没法执行的乱码。
_MD_IMAGE_INLINE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+(?P<text>.+?)\s*$")

# 「## 计算」里的用量行：`西红柿 = 1 个（约 180g） * 份数` / `小葱 2 棵`
_CALC_EQ = re.compile(r"^(?P<name>[^=]+?)\s*[=＝]\s*(?P<amount>.+)$")

# 难度星级 → 预估耗时（分钟）。源仓库模板对星级有明确定义（见 template/示例菜），
# 这里取每档的代表值，比让 AI 猜靠谱。
_STAR_MINUTES = {1: 5, 2: 10, 3: 15, 4: 40, 5: 60}

# 正文里出现的耗时描述，优先于星级估算（更具体）。
# 触发词与数字之间允许夹字：源仓库写法多样 ——「大约只需 15 分钟」
# 「全程耗时约 40 分钟」「预计需要 20 分钟」，卡死相邻会漏掉一大半。
_EXPLICIT_MINUTES = re.compile(r"(?:约|大约|需要|只需|耗时|全程|预计)[^\d]{0,6}(\d{1,3})\s*分钟")

_SKIP_DIRS = {"template", ".git", "node_modules"}


def _strip_comments(text: str) -> str:
    return _HTML_COMMENT.sub("", text)


def _split_sections(content: str) -> tuple[str, str, dict[str, list[str]]]:
    """→ (标题, 前言, {二级标题: [正文行]})。

    `## 操作` 下的 `### 小节` 不单独成节：小节名作为步骤的分组前缀并入步骤文本，
    否则「五花肉一段处理」这种上下文会丢，步骤就串味了。
    """
    title = ""
    intro_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None

    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _MD_IMAGE.match(line):
            continue

        if line.startswith("# "):
            m = _TITLE.match(line)
            if m and not title:
                title = m.group("name").strip()
            continue

        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue

        if current is None:
            intro_lines.append(line)
        else:
            sections[current].append(line)

    return title, "\n".join(intro_lines), sections


def _clean_amount(text: str) -> str:
    """用量串收尾清理。

    源仓库的计算行常把括注写在名字一侧：
        `青红椒（根据受辣程度选择, 0-30g）*注：不建议使用肉厚的菜椒`
    等号切分后右边会拖着孤立的右括号和 `*注：…` 尾巴。用量要短、一眼能读，
    这些解释性内容不该混在里面。
    """
    text = (text or "").strip()
    text = re.split(r"\*\s*注[：:]", text)[0].strip()
    # 去掉不成对的收尾括号
    if text.count("）") > text.count("（"):
        text = text.rsplit("）", 1)[0].strip()
    if text.count(")") > text.count("("):
        text = text.rsplit(")", 1)[0].strip()
    return text.strip(" ,，、")


def _parse_ingredients(raw_lines: list[str], calc_lines: list[str]) -> list[Ingredient]:
    """合并「必备原料和工具」与「计算」两段。

    前者给名字（含「（可选）」标注），后者给用量公式。按名字前缀对齐 ——
    源仓库里两段的写法不完全一致（「糖（可选）」vs「糖 = 0-2g * 份数」），
    故用"计算行的名字是否被原料名包含"来匹配，而不是要求完全相等。
    """
    names: list[str] = []
    for line in raw_lines:
        m = _LIST_ITEM.match(line)
        if m:
            names.append(m.group("text").strip())

    # 计算段：名字 → 用量
    amounts: dict[str, str] = {}
    for line in calc_lines:
        m = _LIST_ITEM.match(line)
        if not m:
            continue
        text = m.group("text").strip()
        eq = _CALC_EQ.match(text)
        if eq:
            amounts[eq.group("name").strip()] = _clean_amount(eq.group("amount"))
        else:
            # 「小葱 2 棵」这种没有等号的：第一个空格后当用量
            parts = text.split(None, 1)
            if len(parts) == 2:
                amounts[parts[0].strip()] = _clean_amount(parts[1])

    out: list[Ingredient] = []
    for name in names:
        # 「糖（可选）」→ 主名「糖」，括注进 note
        note = ""
        m = re.match(r"^(?P<base>[^（(]+)[（(](?P<note>[^）)]*)[）)]\s*$", name)
        base = name
        if m:
            base = m.group("base").strip()
            note = m.group("note").strip()

        amount = ""
        for key, val in amounts.items():
            if key == base or key.startswith(base) or base.startswith(key):
                amount = val
                break
        out.append(Ingredient(name=base, amount=amount, note=note))

    # 计算段里有、原料段没有的，补进去（偶有遗漏）
    known = {i.name for i in out}
    for key, val in amounts.items():
        if key not in known and not any(key.startswith(k) or k.startswith(key) for k in known):
            out.append(Ingredient(name=key, amount=val))

    return [i for i in out if i.name]


def _parse_steps(lines: list[str]) -> list[RecipeStep]:
    """解析「## 操作」。

    - `### 小节` 作为分组前缀并入步骤文本（保住上下文）
    - 缩进的 `- 说明` 是上一步的补充，用「※」接到该步后面，不单独成步 ——
      它们不是独立操作，单独成步会让 Kindle 上的步骤号虚增、看着像更多活要干
    """
    steps: list[RecipeStep] = []
    group = ""

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        if line.startswith("### "):
            group = line[4:].strip()
            continue

        indented = line.startswith("  ") or line.startswith("\t")
        m = _LIST_ITEM.match(line)
        if not m:
            continue
        text = _MD_IMAGE_INLINE.sub("", m.group("text")).strip()
        if not text:
            # 整行只有图片（`![成品](./x.jpg)`），不是步骤
            continue

        if indented and steps:
            steps[-1].text = f"{steps[-1].text}　※ {text}"
            continue

        if group:
            text = f"【{group}】{text}"
        steps.append(RecipeStep(order=len(steps) + 1, text=text))

    return steps


def _estimate_minutes(intro: str, stars: int) -> Optional[int]:
    m = _EXPLICIT_MINUTES.search(intro)
    if m:
        return int(m.group(1))
    return _STAR_MINUTES.get(stars)


def build_recipe(
    content: str,
    category: str = "",
    fallback_title: str = "",
    source: str = "howtocook",
    source_url: str = "",
) -> Recipe:
    """HowToCook Markdown → Recipe。"""
    content = _strip_comments(content)
    title, intro, sections = _split_sections(content)
    title = title or fallback_title or "未命名"

    ingredients = _parse_ingredients(
        sections.get("必备原料和工具", []), sections.get("计算", [])
    )
    steps = _parse_steps(sections.get("操作", []))

    stars = 0
    m = _DIFFICULTY.search(content)
    if m:
        stars = len(m.group("stars"))
    kcal = None
    m = _CALORIE.search(content)
    if m:
        try:
            kcal = float(m.group("kcal"))
        except ValueError:
            kcal = None

    # 贴士 = 附加内容 + 难度/热量摘要。源仓库每篇末尾都有一句"请提 Issue"的
    # 号召，对做饭没用，去掉。
    tips_parts: list[str] = []
    if stars:
        bits = [f"难度 {'★' * stars}"]
        if kcal:
            bits.append(f"约 {kcal:g} 大卡")
        tips_parts.append(" · ".join(bits))
    for line in sections.get("附加内容", []):
        m = _LIST_ITEM.match(line)
        text = _MD_IMAGE_INLINE.sub("", m.group("text") if m else line).strip()
        if not text or "Issue" in text or "Pull request" in text:
            continue
        tips_parts.append(f"· {text}")

    tags = derive_tags(
        title=title,
        category=category,
        ingredients=[f"{i.amount}{i.name}{i.note}" for i in ingredients],
        steps=[s.text for s in steps],
    )

    return Recipe(
        slug=make_slug(title, source),
        title=title,
        category=category,
        ingredients=ingredients,
        steps=steps,
        method_tags=tags["method"],
        flavor_tags=tags["flavor"],
        ingredient_tags=tags["ingredient"],
        total_minutes=_estimate_minutes(intro, stars),
        tips="\n".join(tips_parts),
        source=source,
        source_url=source_url,
        raw_text=content,
        parse_status="parsed" if steps else "needs_convert",
    )


def iter_recipe_files(root: Path) -> Iterable[tuple[Path, str]]:
    """遍历 dishes/ 目录，产出 (文件, 分类)。"""
    dishes = root / "dishes"
    if not dishes.exists():
        dishes = root
    for path in sorted(dishes.rglob("*.md")):
        rel = path.relative_to(dishes)
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            continue
        if path.name.lower() in ("readme.md",):
            continue
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        yield path, CATEGORY_MAP.get(top, "")


def import_from_directory(
    root: str | Path,
    source: str = "howtocook",
    url_prefix: str = "https://github.com/Anduin2017/HowToCook/blob/master",
    overwrite: bool = False,
) -> dict:
    """把一个本地 HowToCook 克隆导入库中。"""
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
        "HowToCook 导入完成: 新增 %d, 跳过 %d, 失败 %d, 待转换 %d",
        imported, skipped, failed, len(needs_convert),
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "needs_convert": needs_convert,
    }
