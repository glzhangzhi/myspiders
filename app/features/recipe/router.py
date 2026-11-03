"""菜谱 API 路由 —— 只解析转发，业务在 services/recipe_service.py。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.security import require_admin_token
from app.features.recipe.vocab_repo import GROUP_LABELS, tag_vocab_repo
from app.features.recipe.repo import recipe_repo
from app.features.recipe import kindle as recipe_kindle, service as recipe_service

router = APIRouter(prefix="/api/recipes", tags=["Recipe"])


class RecipePayload(BaseModel):
    title: str = ""
    category: str = ""
    ingredients: list[dict] = []
    steps: list = []
    method_tags: list[str] = []
    flavor_tags: list[str] = []
    ingredient_tags: list[str] = []
    custom_tags: list[str] = []
    servings: str = ""
    total_minutes: Optional[int] = None
    tips: str = ""
    source: str = "manual"
    source_url: str = ""
    raw_text: str = ""


class ConvertRequest(BaseModel):
    text: str
    store: bool = True
    source_url: str = ""


class MarkdownRequest(BaseModel):
    content: str
    category: str = ""
    title: str = ""


class MenuRequest(BaseModel):
    slugs: list[str] = []
    note: str = ""


class TagRequest(BaseModel):
    tag: str


class TagRenameRequest(BaseModel):
    old: str
    new: str


class FavoriteRequest(BaseModel):
    favorite: bool


class SyncRequest(BaseModel):
    overwrite: bool = False


class PurgeRequest(BaseModel):
    source: str
    keep_slugs: list[str] = []


def _split(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _filters(
    methods: Optional[str] = None,
    flavors: Optional[str] = None,
    ingredients: Optional[str] = None,
    custom: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    favorite: Optional[bool] = None,
    source: Optional[str] = None,
    mine: Optional[bool] = None,
) -> dict:
    return dict(
        methods=_split(methods), flavors=_split(flavors),
        ingredients=_split(ingredients), custom=_split(custom),
        category=category, search=search, favorite=favorite, source=source,
        mine=mine,
    )


# ── 标签词表 ──


@router.get("/tags", summary="可用标签词表")
def tag_vocabulary():
    """前端筛选器渲染用。词表以库为准（用户可在标签管理里增删改）。

    custom 不在此列 —— 它是完全自由的，走 /facets 动态取。
    """
    return tag_vocab_repo.all_groups()


# ── 菜谱查询 ──


@router.get("/list", summary="分页列出菜谱")
def list_recipes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    methods: Optional[str] = None,
    flavors: Optional[str] = None,
    ingredients: Optional[str] = None,
    custom: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    favorite: Optional[bool] = None,
    source: Optional[str] = None,
    mine: Optional[bool] = None,
    sort_by: str = Query("created_at", pattern="^(created_at|title|last_pushed_at|total_minutes)$"),
    sort_order: int = Query(-1, ge=-1, le=1),
):
    common = _filters(methods, flavors, ingredients, custom, category, search, favorite, source, mine)
    return {
        "items": recipe_repo.list_all(
            skip=skip, limit=limit, sort_by=sort_by,
            sort_order=sort_order if sort_order in (1, -1) else -1,
            **common,
        ),
        "total": recipe_repo.count(**common),
        "skip": skip,
        "limit": limit,
    }


@router.get("/facets", summary="当前筛选下各标签计数")
def facets(
    methods: Optional[str] = None,
    flavors: Optional[str] = None,
    ingredients: Optional[str] = None,
    custom: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    favorite: Optional[bool] = None,
    source: Optional[str] = None,
    mine: Optional[bool] = None,
):
    return recipe_repo.facet_counts(
        **_filters(methods, flavors, ingredients, custom, category, search, favorite, source, mine)
    )


@router.get("/stats", summary="统计")
def stats():
    return recipe_repo.stats()


@router.get("/random", summary="随机抽几道（选择困难时用）")
def random_recipes(
    n: int = Query(3, ge=1, le=10),
    methods: Optional[str] = None,
    flavors: Optional[str] = None,
    ingredients: Optional[str] = None,
    custom: Optional[str] = None,
    category: Optional[str] = None,
    favorite: Optional[bool] = None,
    mine: Optional[bool] = None,
):
    return {"items": recipe_repo.random_pick(
        n, **_filters(methods, flavors, ingredients, custom, category, None, favorite, None, mine)
    )}


# ── 推送列表（路径沿用 /menu；必须在 /{slug} 之前注册，否则 "menu" 会被当作 slug）──


@router.get("/menu", summary="推送列表")
def get_menu():
    return recipe_service.get_menu()


@router.post("/menu", summary="保存推送列表", dependencies=[Depends(require_admin_token)])
def save_menu(payload: MenuRequest):
    return recipe_service.save_menu(payload.slugs, payload.note)


@router.get("/menu/shopping", summary="推送列表的配料汇总（采买清单）")
def menu_shopping_list():
    return recipe_service.get_menu_shopping_list()


# ── 标签词表管理 ──


@router.get("/vocab", summary="所有标签组（含使用计数）")
def get_vocab():
    groups = tag_vocab_repo.all_groups()
    return {
        "groups": groups,
        "labels": GROUP_LABELS,
        "usage": {g: tag_vocab_repo.usage_counts(g) for g in groups},
    }


@router.post("/vocab/{group}/add", summary="新增标签", dependencies=[Depends(require_admin_token)])
def vocab_add(group: str, payload: TagRequest):
    try:
        return {"tags": tag_vocab_repo.add(group, payload.tag)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/vocab/{group}/rename", summary="重命名标签（同步改写所有菜谱）", dependencies=[Depends(require_admin_token)])
def vocab_rename(group: str, payload: TagRenameRequest):
    try:
        return {"tags": tag_vocab_repo.rename(group, payload.old, payload.new)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/vocab/{group}/{tag}", summary="删除标签（同步从菜谱摘掉）", dependencies=[Depends(require_admin_token)])
def vocab_remove(group: str, tag: str):
    try:
        return {"tags": tag_vocab_repo.remove(group, tag)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Kindle 推送 ──


@router.get("/kindle/status", summary="Kindle 在线状态")
def kindle_status(refresh: bool = False):
    """默认走 60 秒的过期后台刷新缓存（Kindle 休眠时 ssh 要等满超时，页面不该等它）；
    `refresh=true`（「检测」按钮）实时探测。"""
    return recipe_kindle.kindle_status(force=refresh)


@router.get("/kindle/preview", summary="预览将推送的文件（每道菜一个）")
def kindle_preview():
    return {"files": recipe_service.preview_menu_files()}


@router.post("/kindle/push", summary="推送列表里的菜逐个推到 Kindle", dependencies=[Depends(require_admin_token)])
def kindle_push():
    result = recipe_service.push_menu_to_kindle()
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "推送失败"))
    return result


@router.post("/kindle/push/{slug}", summary="只推一道菜", dependencies=[Depends(require_admin_token)])
def kindle_push_single(slug: str):
    result = recipe_service.push_single_to_kindle(slug)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "推送失败"))
    return result


# ── 导入 / AI 转换 ──


@router.post("/import/howtocook", summary="同步 HowToCook 仓库（家常菜，推荐）", dependencies=[Depends(require_admin_token)])
def import_howtocook(payload: SyncRequest):
    result = recipe_service.sync_howtocook(overwrite=payload.overwrite)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "导入失败"))
    return result


@router.post("/import/cooklikehoc", summary="同步 CookLikeHOC 仓库（商用配方，不推荐）", dependencies=[Depends(require_admin_token)])
def import_cooklikehoc(payload: SyncRequest):
    result = recipe_service.sync_cooklikehoc(overwrite=payload.overwrite)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "导入失败"))
    return result


@router.post("/purge", summary="按来源批量删除菜谱", dependencies=[Depends(require_admin_token)])
def purge(payload: PurgeRequest):
    """清库入口。keep_slugs 里的菜谱会被保留（挑出来的好菜不跟着陪葬）。"""
    if not payload.source:
        raise HTTPException(status_code=400, detail="必须指定 source")
    return recipe_service.purge_by_source(payload.source, keep_slugs=payload.keep_slugs)


@router.post("/import/markdown", summary="粘贴 Markdown 菜谱（规则解析，不花额度）", dependencies=[Depends(require_admin_token)])
def import_markdown(payload: MarkdownRequest):
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="内容不能为空")
    return recipe_service.import_single_markdown(
        payload.content, category=payload.category, title=payload.title
    )


@router.post("/convert", summary="DeepSeek 转换任意菜谱文本", dependencies=[Depends(require_admin_token)])
async def convert(payload: ConvertRequest):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    result = await recipe_service.convert_and_store(
        payload.text, store=payload.store, source_url=payload.source_url
    )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "转换失败"))
    return result


@router.post("/{slug}/reconvert", summary="对已有菜谱重跑 AI 转换", dependencies=[Depends(require_admin_token)])
async def reconvert(slug: str):
    result = await recipe_service.reconvert(slug)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "转换失败"))
    return result


# ── 单条读写（放最后：/{slug} 是通配，会吞掉所有同级路径）──


@router.get("/{slug}", summary="获取单个菜谱")
def get_recipe(slug: str):
    doc = recipe_repo.get(slug)
    if not doc:
        raise HTTPException(status_code=404, detail="菜谱不存在")
    return doc


@router.post("", summary="手动新增菜谱", dependencies=[Depends(require_admin_token)])
def create_recipe(payload: RecipePayload):
    try:
        recipe = recipe_service.create_manual(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return recipe.model_dump(mode="json")


@router.patch("/{slug}", summary="编辑菜谱", dependencies=[Depends(require_admin_token)])
def patch_recipe(slug: str, payload: dict):
    doc = recipe_service.update_recipe(slug, payload)
    if not doc:
        raise HTTPException(status_code=404, detail="菜谱不存在")
    return doc


@router.post("/{slug}/retag", summary="重算派生标签", dependencies=[Depends(require_admin_token)])
def retag_recipe(slug: str):
    doc = recipe_service.retag(slug)
    if not doc:
        raise HTTPException(status_code=404, detail="菜谱不存在")
    return doc


@router.post("/{slug}/favorite", summary="收藏/取消", dependencies=[Depends(require_admin_token)])
def favorite_recipe(slug: str, payload: FavoriteRequest):
    doc = recipe_repo.toggle_favorite(slug, payload.favorite)
    if not doc:
        raise HTTPException(status_code=404, detail="菜谱不存在")
    return doc


@router.delete("/{slug}", summary="删除菜谱", dependencies=[Depends(require_admin_token)])
def delete_recipe(slug: str):
    if not recipe_repo.delete(slug):
        raise HTTPException(status_code=404, detail="菜谱不存在")
    return {"ok": True}
