"""酒馆资料管理 API —— 只解析转发，业务在 services/tavern_{catalog,sync_service}.py。

读接口（搜索/列表）也要 token：酒馆里放的是什么内容，本身就是隐私。
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.security import require_admin_token
from app.features.tavern.catalog import ALL_TYPES, TYPE_CHARACTER, tavern_catalog
from app.features.tavern.chat_ops import TavernOpError, tavern_chat_ops
from app.features.tavern.chats import FORMATS, TavernChatError, tavern_chats
from app.features.tavern.sync import TavernSyncError, tavern_sync_service

router = APIRouter(
    prefix="/api/tavern",
    tags=["Tavern"],
    dependencies=[Depends(require_admin_token)],
)


class AddRequest(BaseModel):
    name: str
    type: str
    alias: Optional[str] = None


class RemoveRequest(BaseModel):
    name: str
    # 默认只从管理列表里摘掉，不动本地文件 —— 删文件是另一个决定，
    # 而且拉下来的 yaml 很可能是用户（或 AI）改过的，误删没法恢复。
    delete_local: bool = False


class SyncRequest(BaseModel):
    # 省略 name = 对所有配置操作（tavern_sync 的 'all'）
    name: Optional[str] = None
    # None = 用该动作的默认值（push 默认强制，pull 默认不强制）；
    # 显式传 true/false 则以调用方为准。
    force: Optional[bool] = None


class ChatRef(BaseModel):
    """一份聊天记录的定位：卡名 + 文件名（含 .jsonl）。"""

    card: str
    file: str


class ChatBatchRequest(BaseModel):
    items: list[ChatRef]


class ChatRenameRequest(BaseModel):
    card: str
    file: str
    title: str   # 新名称，不含 .jsonl


# ── 酒馆侧：搜索 ──


@router.get("/search", summary="按名称搜索酒馆里的角色卡/世界书/预设")
def search(
    q: str = Query("", description="搜索关键词，空则返回空列表"),
    type: Optional[str] = Query(None, description="限定类型；省略则搜全部"),
    limit: int = Query(30, ge=1, le=100),
    refresh: bool = Query(False, description="跳过缓存重新扫描"),
):
    if type and type not in ALL_TYPES:
        raise HTTPException(status_code=400, detail=f"未知类型: {type}")
    types = [type] if type else None
    return {
        "query": q,
        "results": tavern_catalog.search(q, types=types, limit=limit, force=refresh),
    }


@router.get("/catalog", summary="酒馆资料概况（各类型数量与目录可用性）")
def catalog(refresh: bool = Query(False)):
    return {
        "counts": tavern_catalog.counts(force=refresh),
        "availability": tavern_catalog.availability(),
    }


# ── 管理列表 ──


@router.get("/entries", summary="当前纳入管理的配置")
def list_entries():
    try:
        entries = tavern_sync_service.list_entries()
    except TavernSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 聊天记录条数在这里补上，而不是塞进 tavern_sync_service ——
    # 那个模块管的是 tavern_sync.yaml，聊天记录不归它管，合并纯粹是为了
    # 让前端一次请求就能把列表画全（否则每行一次请求）。
    for entry in entries:
        entry["chat_count"] = (
            tavern_chats.count_chats(entry["name"])
            if entry["type"] == TYPE_CHARACTER
            else 0
        )
    return {"entries": entries}


@router.post("/entries", summary="添加要管理的配置")
def add_entry(payload: AddRequest):
    try:
        return tavern_sync_service.add_entry(payload.name, payload.type, payload.alias)
    except TavernSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/entries/remove", summary="移除管理的配置")
def remove_entry(payload: RemoveRequest):
    try:
        return tavern_sync_service.remove_entry(
            payload.name, delete_local=payload.delete_local
        )
    except TavernSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── 同步 ──


@router.get("/status", summary="同步环境状态")
def status():
    return tavern_sync_service.status()


@router.post("/pull", summary="从酒馆拉取到本地")
def pull(payload: SyncRequest):
    return _run("pull", payload)


@router.post("/push", summary="把本地推送到酒馆")
def push(payload: SyncRequest):
    return _run("push", payload)


def _run(action: str, payload: SyncRequest) -> dict:
    # push 默认强制：用户的工作流是「本地改、推回酒馆」，本地即唯一事实来源。
    # 不强制时 tavern_sync 会因为「酒馆里还没有这张卡」而拒绝推送（新建的卡
    # 永远推不上去），也会因为条目数量对不上而拒绝。
    # pull 方向相反 —— 默认不强制，免得把本地未推送的改动冲掉。
    force = payload.force
    if force is None:
        force = action == "push"
    try:
        return tavern_sync_service.run_sync(action, payload.name, force=force)
    except TavernSyncError as exc:
        # 409：和「已有任务在跑」「酒馆网页没开」都是状态冲突，不是请求写错了
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── 本地文件 ──


@router.get("/local", summary="某条配置在本地的文件树")
def local_tree(
    name: str = Query(...),
    type: str = Query(...),
):
    if type not in ALL_TYPES:
        raise HTTPException(status_code=400, detail=f"未知类型: {type}")
    try:
        return tavern_sync_service.local_tree(name, type)
    except TavernSyncError as exc:
        # name 越界（`../`）会在这里被拦下 —— 不接住就是 500
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/local/file", summary="读取本地文件内容")
def local_file(
    name: str = Query(...),
    type: str = Query(...),
    path: str = Query(...),
):
    if type not in ALL_TYPES:
        raise HTTPException(status_code=400, detail=f"未知类型: {type}")
    try:
        return {"path": path, "content": tavern_sync_service.read_local_file(name, type, path)}
    except TavernSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── 聊天记录 ──
#
# 聊天记录不归 tavern_sync 管（它同步的是卡/世界书/预设的内容），所以这一组
# 直接读酒馆的 chats 目录，只读、不抢同步锁。


def _attachment(filename: str) -> str:
    """Content-Disposition。

    文件名基本都是中文（卡名 + 聊天标题），必须走 RFC 5987 的 `filename*`，
    否则非 ASCII 字节直接放进 header 会被 uvicorn 以 latin-1 编码时抛错。
    同时留一个 ASCII 的 `filename` 兜底给老客户端。
    """
    return f"attachment; filename=\"download\"; filename*=UTF-8''{quote(filename)}"


@router.get("/chats", summary="某张角色卡的聊天记录列表")
def list_chats(name: str = Query(..., description="角色卡在酒馆中的名称")):
    try:
        return tavern_chats.list_chats(name)
    except TavernChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chats/preview", summary="预览一份聊天记录的开头")
def preview_chat(
    name: str = Query(...),
    file: str = Query(..., description="聊天记录文件名（含 .jsonl）"),
    limit: int = Query(60, ge=1, le=300),
):
    try:
        return tavern_chats.preview(name, file, limit=limit)
    except TavernChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chats/export", summary="导出一份聊天记录")
def export_chat(
    name: str = Query(...),
    file: str = Query(...),
    format: str = Query("md", description=f"导出格式：{'/'.join(FORMATS)}"),
):
    try:
        filename, body = tavern_chats.export_one(name, file, format)
    except TavernChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _attachment(filename)},
    )


@router.get("/chats/export-all", summary="打包导出某张卡的全部聊天记录")
def export_all_chats(
    name: str = Query(...),
    format: str = Query("md", description=f"导出格式：{'/'.join(FORMATS)}"),
):
    try:
        filename, blob = tavern_chats.export_all(name, format)
    except TavernChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": _attachment(filename)},
    )


# ── 聊天记录：总表与管理 ──
#
# 总表是只读的（直接扫文件），管理是写（必须调酒馆 HTTP API）——
# 分属 tavern_chats 与 tavern_chat_ops 两个模块，纪律不同，见各自文档。


@router.get("/chats/overview", summary="全部聊天记录总表（按卡分组，含楼层与我说了几句）")
def chats_overview(refresh: bool = Query(False, description="跳过缓存重新扫描")):
    return tavern_chats.overview(force=refresh)


@router.get("/chats/ops-status", summary="写通道状态（酒馆 HTTP 是否可达、归档目标）")
def chats_ops_status():
    return tavern_chat_ops.status()


@router.post("/chats/rename", summary="重命名一份聊天记录")
def rename_chat(payload: ChatRenameRequest):
    try:
        return tavern_chat_ops.rename(payload.card, payload.file, payload.title)
    except (TavernOpError, TavernChatError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chats/delete", summary="批量删除聊天记录（不可恢复）")
def delete_chats(payload: ChatBatchRequest):
    # 逐份的成败在返回体里，不用 HTTP 状态码表达 —— 20 份里错 1 份
    # 既不是成功也不是失败，前端要的是两张清单。
    # 只有「整批都没法开始」（如没选中、超上限）才抛 400。
    try:
        return tavern_chat_ops.delete([i.model_dump() for i in payload.items])
    except (TavernOpError, TavernChatError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chats/sync", summary="把选中的聊天记录原样同步到 rclone")
def sync_chats(payload: ChatBatchRequest):
    try:
        return tavern_chat_ops.sync([i.model_dump() for i in payload.items])
    except (TavernOpError, TavernChatError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
