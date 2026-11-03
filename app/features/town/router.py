"""AI 小镇 API —— 只解析转发，业务在 services/town_engine。

读接口不鉴权（与面板其他只读接口一致，外层有 Cloudflare Access）；
任何会花钱或改数据的接口都要 admin token。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.security import require_admin_token
from app.features.town.repo import get_town_repo
from app.features.town import engine as E
from app.features.town import lore as L
from app.features.tavern.catalog import TYPE_CHARACTER, tavern_catalog

router = APIRouter(prefix="/api/town", tags=["Town"])
ADMIN = [Depends(require_admin_token)]


class Member(BaseModel):
    kind: str = "card"     # card | persona
    file: str


class CreateRequest(BaseModel):
    name: str = ""
    members: list[Member]


class PlacesRequest(BaseModel):
    places: list[dict]


class RollbackRequest(BaseModel):
    to_day: int


class OverrideRequest(BaseModel):
    uid: str
    value: Optional[str] = None   # on | off | null（恢复自动判断）


def _err(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ── 选人 ──


@router.get("/cards", summary="搜索酒馆角色卡（建镇选人用）")
def search_cards(q: str = Query(""), limit: int = Query(30, ge=1, le=100)):
    hits = tavern_catalog.search(q, types=[TYPE_CHARACTER], limit=limit)
    return {"results": hits}


@router.get("/personas", summary="酒馆里的用户人设")
def personas():
    return {"results": L.list_personas()}


@router.get("/avatar", summary="角色卡 / 人设头像（原图）")
def avatar(kind: str = Query("card"), file: str = Query(...)):
    try:
        path = L.persona_avatar_path(file) if kind == "persona" else L.card_path(file)
    except ValueError as exc:
        raise _err(exc) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="没有这个文件")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ── 镇 ──


@router.get("/towns", summary="小镇列表")
def list_towns():
    repo = get_town_repo()
    out = []
    for t in repo.list_towns():
        rs = repo.residents_of(t["_id"])
        out.append({"id": t["_id"], "name": t["name"], "day": t.get("day", 0),
                    "cost": t.get("cost"), "created_at": t.get("created_at"),
                    "residents": [r["name"] for r in rs], "running": E.is_running(t["_id"])})
    return E.jsonable({"towns": out})


@router.post("/towns", summary="建镇", dependencies=ADMIN)
def create(payload: CreateRequest):
    try:
        tid = E.create_town(payload.name, [m.model_dump() for m in payload.members])
    except E.TownError as exc:
        raise _err(exc) from exc
    return {"id": tid}


@router.get("/towns/{town_id}", summary="小镇全貌（地图、居民、时间线）")
def get_town(town_id: str):
    try:
        return E.jsonable(E.town_view(town_id))
    except E.TownError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/towns/{town_id}", summary="删除小镇（连同全部日记）", dependencies=ADMIN)
def delete(town_id: str):
    if E.is_running(town_id):
        raise HTTPException(status_code=409, detail="正在进行中，不能删除")
    get_town_repo().delete_town(town_id)
    return {"ok": True}


@router.post("/towns/{town_id}/step", summary="过一天（后台进行，轮询进度）", dependencies=ADMIN)
async def step(town_id: str):
    try:
        return E.start_day(town_id)
    except E.TownError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/towns/{town_id}/days/{day}", summary="某一天的完整内容")
def get_day(town_id: str, day: int):
    try:
        return E.jsonable(E.day_view(town_id, day))
    except E.TownError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/towns/{town_id}/rollback", summary="回退到第 N 天结束时（删掉之后的一切）", dependencies=ADMIN)
def rollback(town_id: str, payload: RollbackRequest):
    try:
        return E.rollback(town_id, payload.to_day)
    except E.TownError as exc:
        raise _err(exc) from exc


@router.put("/towns/{town_id}/places", summary="编辑地点", dependencies=ADMIN)
def put_places(town_id: str, payload: PlacesRequest):
    try:
        E.update_places(town_id, payload.places)
    except E.TownError as exc:
        raise _err(exc) from exc
    return {"ok": True}


class LoreRequest(BaseModel):
    lore: str = ""


@router.put("/towns/{town_id}/lore", summary="改写小镇设定（全镇客观事实）", dependencies=ADMIN)
def put_lore(town_id: str, payload: LoreRequest):
    try:
        E.set_lore(town_id, payload.lore)
    except E.TownError as exc:
        raise _err(exc) from exc
    return {"ok": True}


# ── 居民 ──


@router.get("/residents/{rid}", summary="居民详情（设定条目、全部日记）")
def get_resident(rid: str):
    r = get_town_repo().get_resident(rid)
    if not r:
        raise HTTPException(status_code=404, detail="居民不存在")
    return E.jsonable(E.resident_view(r, full=True))


@router.get("/residents/{rid}/profile", summary="此人每次调用都会带上的设定全文")
def get_profile(rid: str):
    try:
        return {"text": E.profile_text(rid)}
    except E.TownError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/residents/{rid}/entries/{uid}", summary="单条世界书条目全文")
def get_entry(rid: str, uid: str):
    try:
        return {"content": E.entry_content(rid, uid)}
    except E.TownError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/residents/{rid}/override", summary="手动开/关某条设定", dependencies=ADMIN)
def override(rid: str, payload: OverrideRequest):
    try:
        E.set_override(rid, payload.uid, payload.value)
    except E.TownError as exc:
        raise _err(exc) from exc
    return {"ok": True}


class NoteRequest(BaseModel):
    note: str = ""


@router.put("/residents/{rid}/note", summary="改写此人在小镇里的补充设定", dependencies=ADMIN)
def put_note(rid: str, payload: NoteRequest):
    try:
        E.set_note(rid, payload.note)
    except E.TownError as exc:
        raise _err(exc) from exc
    return {"ok": True}


@router.post("/residents/{rid}/resnapshot", summary="从酒馆重新读取设定", dependencies=ADMIN)
def resnapshot(rid: str):
    try:
        r = E.resnapshot(rid)
    except E.TownError as exc:
        raise _err(exc) from exc
    return E.jsonable(E.resident_view(r))
