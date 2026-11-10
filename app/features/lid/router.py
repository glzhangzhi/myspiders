"""入籍考试刷题 API —— 只解析转发，业务在 service.py。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.core.security import require_admin_token
from app.features.lid import service
from app.features.lid.bank import get_bank, image_path

router = APIRouter(prefix="/api/lid", tags=["lid"])

_LONG = "public, max-age=31536000, immutable"


class AnswerReq(BaseModel):
    qid: str
    chosen: int
    mode: str = "practice"


class StarReq(BaseModel):
    qid: str
    starred: bool = True


class QidReq(BaseModel):
    qid: str


class FinishReq(BaseModel):
    answers: list[Optional[int]]


def _run(fn, *a):
    try:
        return fn(*a)
    except service.LidError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/bank", summary="题库（ETag 校验；?v=<etag> 时长期缓存）")
def bank(request: Request):
    b = get_bank()
    headers = {"ETag": b.etag, "Cache-Control": _LONG if request.query_params.get("v") else "no-cache"}
    if request.headers.get("if-none-match") == b.etag:
        return Response(status_code=304, headers=headers)
    return Response(content=b.raw, media_type="application/json", headers=headers)


@router.get("/img/{name}", summary="题目图片")
def img(name: str):
    p = image_path(name)
    if p is None:
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=2592000"})


@router.get("/state", summary="全部进度 + 今日概况 + 掌握预测")
def state():
    return {**service.state(), "bank_etag": get_bank().etag}


@router.get("/stats", summary="统计")
def stats():
    return service.stats()


@router.post("/answer", summary="练习作答（chosen 为官方顺序下标）", dependencies=[Depends(require_admin_token)])
def answer(p: AnswerReq):
    return _run(service.answer, p.qid, p.chosen, p.mode)


@router.post("/star", summary="收藏/取消", dependencies=[Depends(require_admin_token)])
def star(p: StarReq):
    return _run(service.set_star, p.qid, p.starred)


@router.post("/wrongbook/remove", summary="手动移出错题本", dependencies=[Depends(require_admin_token)])
def wrongbook_remove(p: QidReq):
    return _run(service.remove_from_wrongbook, p.qid)


@router.post("/exam/start", summary="开一场模拟考（服务端抽 30+3）", dependencies=[Depends(require_admin_token)])
def exam_start():
    return _run(service.exam_start)


@router.post("/exam/{exam_id}/finish", summary="交卷判分", dependencies=[Depends(require_admin_token)])
def exam_finish(exam_id: str, p: FinishReq):
    return _run(service.exam_finish, exam_id, p.answers)


@router.get("/exams", summary="模拟考历史成绩")
def exams():
    return {"items": service.exams()}
