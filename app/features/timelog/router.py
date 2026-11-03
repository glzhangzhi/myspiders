"""计时（timelog）API —— 只解析转发，业务在 services/timelog_service.py。

全部要 admin token：panel. 前面虽有 Cloudflare Access，Tailscale 那条备用通道上没有。
时间一律是 epoch **毫秒**整数（与手机端、操作日志同一个单位）。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import require_admin_token
from app.features.timelog import service as svc

router = APIRouter(prefix="/api/timelog", tags=["Timelog"], dependencies=[Depends(require_admin_token)])


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except svc.TimelogError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


# 请求体只做最外层的形状；字段级校验在 timelog_replay.validate_op / service 里，
# 手机与网页走同一套，不在这里再写一份会漂的副本。

class SyncBody(BaseModel):
    ops: list[Any] = Field(default_factory=list)


class Segment(BaseModel):
    s: int
    e: Optional[int] = None


class CreateBody(BaseModel):
    segments: list[Segment]
    name: Optional[str] = None
    type_id: Optional[str] = None
    note: Optional[str] = None
    estimate: Optional[int] = None


class SegmentsBody(BaseModel):
    segments: list[Segment]


class SplitBody(BaseModel):
    at: int


class MergeBody(BaseModel):
    ids: list[str]


class TypeBody(BaseModel):
    name: str
    color: Optional[int] = None


# ── 手机 ──

@router.post("/sync", summary="上传操作（幂等）并取回最新状态")
def sync(body: SyncBody):
    return _call(svc.sync, body.ops)


@router.get("/state", summary="手机启动时要的状态")
def state():
    return svc.state()


@router.post("/mute-today", summary="今天不再提醒")
def mute_today():
    return svc.mute_today()


# ── 网页：查看 ──

@router.get("/day/{day}", summary="本地某一天的记录（含计时中）")
def day(day: str):
    return _call(svc.day, day)


@router.get("/unnamed", summary="待补填的未命名记录")
def unnamed():
    return {"records": svc.unnamed()}


@router.get("/records/{rid}", summary="单条记录（ops=true 带上它的操作日志）")
def get_record(rid: str, ops: bool = False):
    return _call(svc.get_record, rid, ops)


# ── 网页：编辑 ──

@router.post("/records", summary="补记一条已完成的记录")
def create_record(body: CreateBody):
    fields = body.model_dump(exclude={"segments"})
    return _call(svc.create_record, fields, [g.model_dump() for g in body.segments])


@router.patch("/records/{rid}", summary="改名称 / 类型 / 备注 / 预估")
def edit_record(rid: str, body: dict[str, Any]):
    return _call(svc.edit_record, rid, body)


@router.put("/records/{rid}/segments", summary="改时间段")
def set_segments(rid: str, body: SegmentsBody):
    return _call(svc.set_segments, rid, [g.model_dump() for g in body.segments])


@router.delete("/records/{rid}", summary="删除记录")
def delete_record(rid: str):
    _call(svc.delete_record, rid)
    return {"ok": True}


@router.post("/records/{rid}/split", summary="在某时刻拆成两条")
def split_record(rid: str, body: SplitBody):
    return {"records": _call(svc.split_record, rid, body.at)}


@router.post("/merge", summary="合并几条记录")
def merge(body: MergeBody):
    return _call(svc.merge_records, body.ids)


# ── 类型 / 设置 ──

@router.get("/types", summary="类型列表")
def list_types(archived: bool = False):
    return {"types": svc.list_types(archived)}


@router.post("/types", summary="新建类型")
def create_type(body: TypeBody):
    return _call(svc.create_type, body.name, body.color)


@router.patch("/types/{tid}", summary="改名 / 换色 / 排序 / 归档")
def update_type(tid: str, body: dict[str, Any]):
    return _call(svc.update_type, tid, body)


@router.get("/settings", summary="设置")
def get_settings():
    return svc.get_settings()


@router.patch("/settings", summary="改设置")
def update_settings(body: dict[str, Any]):
    return _call(svc.update_settings, body)
