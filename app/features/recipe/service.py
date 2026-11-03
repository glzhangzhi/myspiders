"""菜谱业务逻辑 —— 路由层只解析转发，业务都在这里。"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.core.settings import settings
from app.features.recipe.models import Ingredient, Recipe, RecipeStep
from app.features.recipe.repo import meal_plan_repo, recipe_repo
from app.features.recipe import kindle as recipe_kindle, shopping as recipe_shopping
from app.features.recipe.convert import convert_recipe_text, render_doc_for_convert
from app.features.recipe.howtocook import import_from_directory as htc_import
from app.features.recipe.hoc_import import build_recipe, import_from_directory
from app.features.recipe.tags import derive_tags, make_slug

logger = get_logger("recipe")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.recipe_timezone)


def today_str() -> str:
    return datetime.now(_tz()).strftime("%Y-%m-%d")


def tomorrow_str() -> str:
    return (datetime.now(_tz()) + timedelta(days=1)).strftime("%Y-%m-%d")


# ── 菜谱 CRUD ──


def create_manual(payload: dict) -> Recipe:
    """手动录入一道菜（前端表单）。

    标签留空时自动派生 —— 手动录入时没人愿意逐个勾选标签。
    """
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("菜名不能为空")

    ingredients = [
        Ingredient(
            name=str(i.get("name") or "").strip(),
            amount=str(i.get("amount") or "").strip(),
            note=str(i.get("note") or "").strip(),
        )
        for i in (payload.get("ingredients") or [])
        if str(i.get("name") or "").strip()
    ]
    steps = [
        RecipeStep(order=idx + 1, text=str(s).strip() if isinstance(s, str) else str(s.get("text") or "").strip())
        for idx, s in enumerate(payload.get("steps") or [])
    ]
    steps = [s for s in steps if s.text]
    for idx, s in enumerate(steps, 1):
        s.order = idx

    category = (payload.get("category") or "").strip()
    method = payload.get("method_tags") or []
    flavor = payload.get("flavor_tags") or []
    ingredient_tags = payload.get("ingredient_tags") or []
    if not (method or flavor or ingredient_tags):
        derived = derive_tags(
            title=title, category=category,
            ingredients=[f"{i.amount}{i.name}{i.note}" for i in ingredients],
            steps=[s.text for s in steps],
        )
        method, flavor, ingredient_tags = derived["method"], derived["flavor"], derived["ingredient"]

    source = (payload.get("source") or "manual").strip() or "manual"
    recipe = Recipe(
        slug=payload.get("slug") or make_slug(title, source),
        title=title,
        category=category,
        ingredients=ingredients,
        steps=steps,
        method_tags=method,
        flavor_tags=flavor,
        ingredient_tags=ingredient_tags,
        custom_tags=payload.get("custom_tags") or [],
        servings=(payload.get("servings") or "").strip(),
        total_minutes=payload.get("total_minutes"),
        tips=(payload.get("tips") or "").strip(),
        source=source,
        source_url=(payload.get("source_url") or "").strip(),
        raw_text=(payload.get("raw_text") or "").strip(),
        parse_status=payload.get("parse_status") or "manual",
    )
    recipe_repo.upsert(recipe)
    return recipe


def update_recipe(slug: str, payload: dict) -> Optional[dict]:
    """编辑已有菜谱。只更新传进来的字段，避免前端漏传就把字段清空。"""
    if not recipe_repo.exists(slug):
        return None

    allowed = {
        "title", "category", "servings", "total_minutes", "tips",
        "method_tags", "flavor_tags", "ingredient_tags", "custom_tags",
        "source_url", "favorite",
    }
    fields = {k: v for k, v in payload.items() if k in allowed}

    if "ingredients" in payload:
        fields["ingredients"] = [
            Ingredient(
                name=str(i.get("name") or "").strip(),
                amount=str(i.get("amount") or "").strip(),
                note=str(i.get("note") or "").strip(),
            ).model_dump()
            for i in payload["ingredients"]
            if str(i.get("name") or "").strip()
        ]
    if "steps" in payload:
        texts = [
            (s if isinstance(s, str) else s.get("text") or "").strip()
            for s in payload["steps"]
        ]
        fields["steps"] = [
            RecipeStep(order=i + 1, text=t).model_dump()
            for i, t in enumerate([t for t in texts if t])
        ]
    return recipe_repo.update_fields(slug, fields)


def retag(slug: str) -> Optional[dict]:
    """重算三类派生标签（custom_tags 不动 —— 那是人工领地）。"""
    doc = recipe_repo.get(slug)
    if not doc:
        return None
    derived = derive_tags(
        title=doc.get("title", ""),
        category=doc.get("category", ""),
        ingredients=[
            f"{i.get('amount','')}{i.get('name','')}{i.get('note','')}"
            for i in doc.get("ingredients") or []
        ],
        steps=[s.get("text", "") for s in doc.get("steps") or []],
    )
    return recipe_repo.update_fields(slug, {
        "method_tags": derived["method"],
        "flavor_tags": derived["flavor"],
        "ingredient_tags": derived["ingredient"],
    })


# ── AI 转换 ──


async def convert_and_store(
    text: str,
    store: bool = True,
    source: str = "deepseek",
    source_url: str = "",
) -> dict:
    """自由文本 → 结构化菜谱。store=False 时只预览不落库。"""
    result = await convert_recipe_text(text, source=source, source_url=source_url)
    if not result.ok:
        return {
            "ok": False,
            "error": result.error,
            "raw_reply": result.raw_reply,
            "cost_yuan": round(result.cost_yuan, 5),
        }

    recipe = result.recipe
    if store:
        recipe_repo.upsert(recipe)

    return {
        "ok": True,
        "stored": store,
        "recipe": recipe.model_dump(mode="json"),
        "warnings": result.warnings,
        "answers": result.answers,
        "cost_yuan": round(result.cost_yuan, 5),
        "tokens": {"input": result.input_tokens, "output": result.output_tokens},
    }


async def reconvert(slug: str) -> dict:
    """对库里已有条目重新跑一次 AI 转换。

    喂的是**当前库里的内容**（含用户的编辑与 `[]` 提问），不是导入时的
    raw_text —— 见 render_doc_for_convert 的说明。
    """
    doc = recipe_repo.get(slug)
    if not doc:
        return {"ok": False, "error": "菜谱不存在"}

    text = render_doc_for_convert(doc)
    if not text.strip():
        # 结构全空（只有个标题都没有）时才回落到原始文本
        text = doc.get("raw_text") or ""
    if not text.strip():
        return {"ok": False, "error": "这条菜谱没有任何可转换的内容"}

    result = await convert_recipe_text(
        text, source=doc.get("source") or "deepseek", source_url=doc.get("source_url", "")
    )
    if not result.ok:
        return {"ok": False, "error": result.error, "cost_yuan": round(result.cost_yuan, 5)}

    # 不留快照、不提供还原：一直保留最新版（机主的选择 —— 转换结果不满意就
    # 改一下再转一次，比在两个版本之间来回切更直接）。
    # 关键：保持原 slug。转换后标题可能被模型改写（「西红柿炒蛋」→「番茄炒蛋」），
    # 若跟着换 slug 就会凭空多出一条重复菜谱，旧的还留在排餐里指向空记录。
    new = result.recipe
    new.slug = slug
    new.category = new.category or doc.get("category", "")
    # raw_text 是「导入时的原始文本」，不能被这次拼出来的输入替换掉 ——
    # 它是最后一层人工回看的兜底，一旦被覆盖就永远找不回源数据了。
    new.raw_text = doc.get("raw_text") or new.raw_text
    recipe_repo.upsert(new)
    return {
        "ok": True,
        "recipe": recipe_repo.get(slug),
        "warnings": result.warnings,
        "answers": result.answers,
        "cost_yuan": round(result.cost_yuan, 5),
    }


# ── 导入 CookLikeHOC ──


def _sync_repo(url: str, local_dir: str) -> Optional[str]:
    """克隆或更新一个菜谱仓库。返回错误信息，成功则 None。

    浅克隆：只要最新内容，不需要历史。
    """
    local = Path(local_dir)
    local.parent.mkdir(parents=True, exist_ok=True)

    if not shutil.which("git"):
        return "本机没有 git 命令"

    try:
        if (local / ".git").exists():
            proc = subprocess.run(
                ["git", "-C", str(local), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=180,
            )
        else:
            if local.exists():
                shutil.rmtree(local)
            proc = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(local)],
                capture_output=True, text=True, timeout=300,
            )
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout)[:300]
    except subprocess.TimeoutExpired:
        return "git 操作超时"
    return None


def sync_howtocook(overwrite: bool = False) -> dict:
    """同步 HowToCook（Anduin2017/HowToCook）并导入。

    这是**面向家庭厨房**的源：370 道菜格式完全统一，自带难度分级与热量，
    步骤写给没经验的人看。与老乡鸡那套商用后厨配方正相反。
    """
    err = _sync_repo(settings.howtocook_repo_url, settings.howtocook_local_dir)
    if err:
        return {"ok": False, "error": err}
    stats = htc_import(settings.howtocook_local_dir, overwrite=overwrite)
    return {"ok": True, **stats}


def sync_cooklikehoc(overwrite: bool = False) -> dict:
    """同步 CookLikeHOC（老乡鸡）并导入。

    **保留但不推荐**：实测 340 道里 83% 有硬伤（商用克数 / 蒸柜等设备 /
    依赖中央厨房半成品），真正能直接家用的只有 9 道。留着这个入口是为了
    偶尔捞个别菜，别再全量导入。
    """
    err = _sync_repo(settings.cooklikehoc_repo_url, settings.cooklikehoc_local_dir)
    if err:
        return {"ok": False, "error": err}
    stats = import_from_directory(settings.cooklikehoc_local_dir, overwrite=overwrite)
    return {"ok": True, **stats}


def purge_by_source(source: str, keep_slugs: Optional[list[str]] = None) -> dict:
    """删掉某个来源的全部菜谱（keep_slugs 除外），并清理当前菜单里的悬空引用。"""
    keep = set(keep_slugs or [])
    victims = [
        d["slug"]
        for d in recipe_repo.collection.find({"source": source}, {"slug": 1, "_id": 0})
        if d["slug"] not in keep
    ]
    if not victims:
        return {"deleted": 0, "kept": len(keep)}

    recipe_repo.collection.delete_many({"slug": {"$in": victims}})

    # 当前菜单里若引用了被删的菜，顺手摘掉 —— 否则推送时会静默少菜
    menu = meal_plan_repo.get_current()
    if menu and menu.get("slugs"):
        remaining = [s for s in menu["slugs"] if s not in set(victims)]
        if len(remaining) != len(menu["slugs"]):
            meal_plan_repo.save_current(remaining, menu.get("note", ""))

    logger.info("清理来源 %s: 删除 %d 条，保留 %d 条", source, len(victims), len(keep))
    return {"deleted": len(victims), "kept": len(keep)}


def import_single_markdown(content: str, category: str = "", title: str = "") -> dict:
    """粘贴一份 markdown 菜谱，走规则解析入库（不花 API 额度）。"""
    recipe = build_recipe(content, category=category, fallback_title=title, source="manual")
    recipe_repo.upsert(recipe)
    return recipe.model_dump(mode="json")


# ── 推送列表 ──
#
# 原先按日期存排餐 + 历史列表，实际用不上：做饭是"现在要做这几道菜"，
# 不是日程管理。现在只有**一份**推送列表（库里仍叫 meal_plans/current），
# 选完直接推，每道菜一个文件。


def get_menu() -> dict:
    """取推送列表，附带完整菜谱内容（前端要展示菜名和标签）。"""
    menu = meal_plan_repo.get_current() or {"slugs": [], "note": "", "pushed_at": None}
    menu["recipes"] = recipe_repo.get_many(menu.get("slugs") or [])
    # slug 指向的菜谱被删掉时，前端要能看出来缺了东西
    found = {r["slug"] for r in menu["recipes"]}
    menu["missing_slugs"] = [s for s in (menu.get("slugs") or []) if s not in found]
    return menu


def save_menu(slugs: list[str], note: str = "") -> dict:
    """保存推送列表。去重保序 —— 同一道菜选两次没有意义。"""
    seen: list[str] = []
    for s in slugs:
        if s and s not in seen:
            seen.append(s)
    meal_plan_repo.save_current(seen, note)
    return get_menu()


def get_menu_shopping_list() -> dict:
    """推送列表的配料汇总 —— 采买清单。

    以列表里**实际存在**的菜为准：引用了已删除菜谱时，清单少一道菜总比
    报错强，缺失情况由 get_menu 的 missing_slugs 另行提示。
    """
    menu = get_menu()
    return recipe_shopping.aggregate(menu["recipes"])


# ── 推送到 Kindle ──
#
# 一道菜一个文件、文件名 = 菜名，同名覆盖。批量推送也是逐个文件，不合并 ——
# Kindle 上每道菜永远只有最新一份。


def _render_files(recipes: list[dict], note: str = "") -> list[dict]:
    taken: set = set()
    return [
        {
            "slug": r["slug"],
            "title": r.get("title") or "",
            "filename": recipe_kindle.recipe_filename(
                r.get("title") or "", taken=taken, source=r.get("source") or ""
            ),
            "content": recipe_kindle.render_recipe_file(r, note=note),
        }
        for r in recipes
    ]


def preview_menu_files() -> list[dict]:
    menu = get_menu()
    return _render_files(menu["recipes"], note=menu.get("note", ""))


def _push_files(files: list[dict]) -> dict:
    """逐个推送。第一个就失败时直接停：多半是 Kindle 不在线，后面每个都要
    白等一次 SSH 超时；中途失败则照推剩下的（单个文件的问题不该连坐）。"""
    pushed, failed = [], []
    for i, f in enumerate(files):
        r = recipe_kindle.push_text(f["content"], filename=f["filename"])
        if r.get("ok"):
            pushed.append({"slug": f["slug"], "filename": f["filename"], "path": r["path"]})
        else:
            failed.append({"slug": f["slug"], "filename": f["filename"], "error": r.get("error", "")})
            if i == 0:
                failed.extend(
                    {"slug": x["slug"], "filename": x["filename"], "error": "未尝试（第一个就失败了）"}
                    for x in files[1:]
                )
                break
    if pushed:
        recipe_repo.mark_pushed([p["slug"] for p in pushed])
    return {
        "ok": bool(pushed),
        "pushed": pushed,
        "failed": failed,
        "error": failed[0]["error"] if failed and not pushed else "",
        "dir": settings.kindle_recipe_dir,
    }


def push_menu_to_kindle() -> dict:
    """把推送列表里的每道菜各推成一个文件。"""
    menu = get_menu()
    if not menu["recipes"]:
        return {"ok": False, "error": "推送列表是空的"}
    if menu["missing_slugs"]:
        # 菜谱被删但列表还引用着 —— 静默少推一道菜，人到灶台前才发现，最难受。
        logger.warning(
            "推送列表引用了 %d 道已删除的菜: %s",
            len(menu["missing_slugs"]), menu["missing_slugs"],
        )

    result = _push_files(_render_files(menu["recipes"], note=menu.get("note", "")))
    if result["ok"]:
        meal_plan_repo.mark_pushed()
        result["missing_slugs"] = menu["missing_slugs"]
    return result


def push_single_to_kindle(slug: str) -> dict:
    """只推一道菜（临时起意做某个菜时用）。"""
    doc = recipe_repo.get(slug)
    if not doc:
        return {"ok": False, "error": "菜谱不存在"}
    return _push_files(_render_files([doc]))
