"""账单 / 支出 API —— 只解析转发，业务在 services/finance_service.py。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.core.security import require_admin_token
from app.features.finance.models import TxPatch
from app.features.finance.repo import finance_repo
from app.features.finance import service as finance_service

router = APIRouter(prefix="/api/finance", tags=["Finance"])


class PatchRequest(BaseModel):
    patch: TxPatch
    learn_rule: bool = False


class CategoryRequest(BaseModel):
    l1: str
    color: str = ""


class RenameRequest(BaseModel):
    old: str
    new: str


class SubRequest(BaseModel):
    l1: str
    sub: str
    new: str = ""


class ClosedRequest(BaseModel):
    closed: bool


def _months(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


# ── 读 ──


@router.get("/bootstrap", summary="页面启动所需的全部数据")
def bootstrap():
    """一次取齐：交易、月份进度、分类词表、规则、封账状态。

    前端启动时本来要打 5 个请求，合成一个省掉首屏的瀑布等待；
    数据量也不大（半年 368 笔约 150KB）。
    """
    finance_service.ensure_seeded()
    return {
        "transactions": finance_repo.list_tx(),
        "months": finance_service.month_progress(),
        "categories": finance_repo.list_categories(),
        "rules": finance_repo.list_rules(),
        "closed": finance_repo.closed_months(),
        "statements": finance_repo.list_statements(),
    }


@router.get("/transactions", summary="交易列表")
def list_transactions(
    months: Optional[str] = Query(None, description="逗号分隔，如 2026-03,2026-04"),
    reviewed: Optional[bool] = None,
    kind: Optional[str] = None,
    search: Optional[str] = None,
):
    return {"transactions": finance_repo.list_tx(
        months=_months(months), reviewed=reviewed, kind=kind, search=search,
    )}


@router.get("/summary", summary="汇总聚合")
def summary(
    months: Optional[str] = Query(None),
    only_var: bool = Query(True, description="只统计可变支出（滤掉房租等固定项）"),
):
    ms = _months(months) or finance_repo.months()
    return finance_service.summary(ms, only_var=only_var)


@router.get("/categories", summary="分类词表")
def list_categories():
    finance_service.ensure_seeded()
    return {"categories": finance_repo.list_categories()}


@router.get("/statements", summary="导入批次")
def list_statements():
    return {"statements": finance_repo.list_statements()}


# ── 导入 ──


@router.post("/import", summary="上传对账单 PDF", dependencies=[Depends(require_admin_token)])
async def import_statement(
    file: UploadFile = File(...),
    force: bool = Query(False, description="越过「已导入过」与「勾稽不平」两道闸"),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只接受 PDF")
    finance_service.ensure_seeded()
    # 落临时文件再解析：pdftotext 要的是路径，且大文件不该整份读进内存
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        return finance_service.import_pdf(tmp_path, file_name=file.filename, force=force)
    except FileNotFoundError as exc:
        # 解析依赖外部命令 pdftotext（poppler-utils）；没装时原先是一个裸 500
        if "pdftotext" in str(exc):
            raise HTTPException(status_code=503, detail="服务器缺少 pdftotext，请安装 poppler-utils")
        raise
    finally:
        tmp_path.unlink(missing_ok=True)


@router.delete("/statements/{sha}", summary="删除一次导入及其交易",
               dependencies=[Depends(require_admin_token)])
def delete_statement(sha: str):
    res = finance_repo.delete_statement(sha)
    if not res["deleted"]:
        raise HTTPException(status_code=404, detail="没有这次导入")
    return res


# ── 编辑 ──


@router.patch("/transactions/{tx_id}", summary="编辑一笔交易",
              dependencies=[Depends(require_admin_token)])
def patch_transaction(tx_id: str, payload: PatchRequest):
    res = finance_service.patch_transaction(
        tx_id, payload.patch.model_dump(exclude_none=True), learn_rule=payload.learn_rule,
    )
    if not res["ok"]:
        raise HTTPException(status_code=404, detail=res["reason"])
    return res


@router.post("/months/{month}/closed", summary="封账 / 取消封账",
             dependencies=[Depends(require_admin_token)])
def set_closed(month: str, payload: ClosedRequest):
    finance_repo.set_closed(month, payload.closed)
    return {"ok": True, "closed": finance_repo.closed_months()}


# ── 分类词表 ──


@router.post("/categories", summary="新增大类", dependencies=[Depends(require_admin_token)])
def add_category(payload: CategoryRequest):
    finance_repo.add_category(payload.l1, payload.color)
    return {"categories": finance_repo.list_categories()}


@router.post("/categories/rename", summary="大类改名（同步改写所有引用）",
             dependencies=[Depends(require_admin_token)])
def rename_category(payload: RenameRequest):
    finance_repo.rename_category_vocab(payload.old, payload.new)
    n = finance_repo.rename_category(payload.old, payload.new)
    return {"categories": finance_repo.list_categories(), "updated": n}


@router.delete("/categories/{l1}", summary="删除大类（交易退回未分类）",
               dependencies=[Depends(require_admin_token)])
def delete_category(l1: str):
    n = finance_repo.reset_category(l1)
    finance_repo.delete_category_vocab(l1)
    return {"categories": finance_repo.list_categories(), "updated": n}


@router.post("/categories/sub", summary="新增二级分类",
             dependencies=[Depends(require_admin_token)])
def add_sub(payload: SubRequest):
    cats = {c["l1"]: c for c in finance_repo.list_categories()}
    cur = cats.get(payload.l1)
    if not cur:
        raise HTTPException(status_code=404, detail="大类不存在")
    subs = list(cur.get("subs", []))
    if payload.sub not in subs:
        subs.append(payload.sub)
    finance_repo.set_subs(payload.l1, subs)
    return {"categories": finance_repo.list_categories()}


@router.post("/categories/sub/rename", summary="二级改名（同步改写所有引用）",
             dependencies=[Depends(require_admin_token)])
def rename_sub(payload: SubRequest):
    if not payload.new:
        raise HTTPException(status_code=400, detail="新名称不能为空")
    finance_repo.rename_sub(payload.l1, payload.sub, payload.new)
    n = finance_repo.rename_category(payload.l1, payload.l1, sub=(payload.sub, payload.new))
    return {"categories": finance_repo.list_categories(), "updated": n}


@router.delete("/categories/{l1}/sub/{sub}", summary="删除二级分类",
               dependencies=[Depends(require_admin_token)])
def delete_sub(l1: str, sub: str):
    finance_repo.delete_sub(l1, sub)
    n = finance_repo.clear_sub(l1, sub)
    return {"categories": finance_repo.list_categories(), "updated": n}


# ── 商家规则 ──


@router.get("/rules", summary="商家规则列表")
def list_rules():
    return {"rules": finance_repo.list_rules()}


@router.delete("/rules/{merchant}", summary="删除一条商家规则",
               dependencies=[Depends(require_admin_token)])
def delete_rule(merchant: str):
    return {"deleted": finance_repo.delete_rule(merchant)}
