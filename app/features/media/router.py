"""媒体下载接口（视频号作者 + YouTube 列表）。整组需要 admin token。

全部写成同步 `def`：几乎每个接口都可能 ssh 到那台电脑（最长几十秒），
FastAPI 会把同步路由放进线程池；写成 async 再直接调就会卡住整个事件循环。

长任务（检测 / 下载）立即返回，后台线程执行；前端轮询 `/status` 看进度。
已有任务在跑 → 409；3060ti 离线 → 409 + `offline: true`（前端据此显示灰色而非报红）。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import require_admin_token
from app.core.settings import settings
from app.features.media.repo import get_media_repo
from app.features.media import agent_client as agent
from app.features.media import rules as R
from app.features.media import service as svc

router = APIRouter(prefix="/api/media", tags=["media"], dependencies=[Depends(require_admin_token)])


def _doc(d: dict) -> dict:
    d = dict(d)
    d["_id"] = str(d["_id"])
    d["id"] = d["_id"]
    return d


def _guard_online() -> None:
    st = agent.ping()
    if not st.get("online"):
        raise HTTPException(409, detail={"offline": True, "msg": "3060ti 离线：开机后再试"})


@router.get("/status", summary="电脑在线状态 + 当前运行 + 计数")
def status():
    return svc.status()


@router.post("/ping", summary="立刻重新探测电脑状态（不读缓存）")
def ping():
    return agent.ping(True)


# ── 来源 ─────────────────────────────────────────────────────────────────

class SourceIn(BaseModel):
    platform: str
    input: str = ""
    pick: Optional[dict] = None


class SourcePatch(BaseModel):
    mode: Optional[str] = None
    paused: Optional[bool] = None
    pick_username: Optional[str] = None
    name: Optional[str] = None


@router.get("/sources")
def list_sources():
    repo = get_media_repo()
    counts = repo.count_by_source()
    return {"items": [{**_doc(s), "counts": counts.get(s["_id"], {})} for s in repo.list_sources()]}


@router.post("/sources")
def add_source(body: SourceIn):
    if body.platform not in ("wx", "yt"):
        raise HTTPException(400, "platform 只能是 wx 或 yt")
    try:
        return _doc(svc.add_source(body.platform, body.input, body.pick))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _mode_all_fields(src: dict) -> dict:
    """切到「全部」= 要这个作者的全部历史。视频号要另外翻页补全（`full_done=False`
    让下次检测翻到底）；YouTube 列表每次本来就读全量，不需要。"""
    fields: dict = {"mode": "all"}
    if src.get("platform") == "wx" and src.get("mode") != "all":
        fields["full_done"] = False
    return fields


def _queue_history(repo, src: dict) -> int:
    """「补下历史」：已跳过的立刻进队列，且切到「全部」让下次检测把还没见过的更早作品也拉进来。
    （此前只把首次检测那一页的 15 条放回队列 —— 更早的作品根本没被检测过。）"""
    ids = [d["_id"] for d in repo.list_items(status=R.SKIPPED, source_id=src["_id"], limit=5000)]
    n = repo.set_status(ids, R.PENDING, only_from=[R.SKIPPED])
    repo.patch_source(src["_id"], _mode_all_fields(src))
    return n


class SourceBatch(BaseModel):
    ids: List[str]
    action: str            # mode_all | mode_new | pause | resume | delete | queue_skipped


@router.post("/sources/batch", summary="多选来源的批量操作")
def batch_sources(body: SourceBatch):
    """前端 Ctrl/Shift 多选后一次提交。注意要注册在 `/sources/{sid}` 之前。"""
    repo = get_media_repo()
    n, extra = 0, 0
    for sid in body.ids:
        src = repo.get_source(sid)
        if not src:
            continue
        a = body.action
        if a == "mode_all":
            repo.patch_source(sid, _mode_all_fields(src))
        elif a == "mode_new":
            repo.patch_source(sid, {"mode": "new_only"})
        elif a == "pause":
            repo.patch_source(sid, {"status": "paused"})
        elif a == "resume":
            repo.patch_source(sid, {"status": "active" if src.get("key") else "unresolved"})
        elif a == "delete":
            extra += repo.delete_source(sid)["deleted_items"]
        elif a == "queue_skipped":
            extra += _queue_history(repo, src)
        else:
            raise HTTPException(400, f"未知操作 {a}")
        n += 1
    return {"n": n, "items": extra}


@router.patch("/sources/{sid}")
def patch_source(sid: str, body: SourcePatch):
    repo = get_media_repo()
    src = repo.get_source(sid)
    if not src:
        raise HTTPException(404, "来源不存在")
    if body.pick_username:
        return _doc(svc.pick_candidate(sid, body.pick_username))
    fields: dict = {}
    if body.mode is not None:
        if body.mode not in ("new_only", "all"):
            raise HTTPException(400, "mode 只能是 new_only 或 all")
        fields.update(_mode_all_fields(src) if body.mode == "all" else {"mode": "new_only"})
    if body.paused is not None:
        fields["status"] = "paused" if body.paused else ("active" if src.get("key") else "unresolved")
    if body.name is not None:
        fields["name"] = body.name.strip()
    return _doc(repo.patch_source(sid, fields) or src)


@router.delete("/sources/{sid}")
def delete_source(sid: str):
    return get_media_repo().delete_source(sid)


@router.post("/sources/{sid}/queue-skipped", summary="把这个来源「已跳过」的历史作品全部加回待下载")
def queue_skipped(sid: str):
    repo = get_media_repo()
    src = repo.get_source(sid)
    if not src:
        raise HTTPException(404, "来源不存在")
    return {"n": _queue_history(repo, src)}


@router.get("/wx/search", summary="搜视频号作者；keyword 为空 = 列出我关注的全部作者（需要视频号页面已连接）")
def wx_search(keyword: str = ""):
    _guard_online()
    try:
        return svc.search_wx(keyword)
    except agent.AgentOffline:
        raise HTTPException(409, detail={"offline": True, "msg": "3060ti 离线"})


# ── 条目 ─────────────────────────────────────────────────────────────────

class IdsIn(BaseModel):
    ids: List[str] = []


@router.get("/items")
def list_items(status: Optional[str] = None, platform: Optional[str] = None,
               source_id: Optional[str] = None, limit: int = 200, skip: int = 0):
    repo = get_media_repo()
    limit = max(1, min(limit, 1000))
    return {"items": [_doc(d) for d in repo.list_items(status, platform, source_id, limit, skip)],
            "counts": repo.count_by_status()}


@router.post("/items/skip")
def skip_items(body: IdsIn):
    return {"n": get_media_repo().set_status(body.ids, R.SKIPPED, only_from=[R.PENDING, R.FAILED])}


@router.post("/items/requeue")
def requeue_items(body: IdsIn):
    return {"n": get_media_repo().set_status(body.ids, R.PENDING,
                                             only_from=[R.SKIPPED, R.FAILED, R.DONE])}


class ClearIn(BaseModel):
    status: str


@router.post("/items/clear", summary="清空待下载 / 失败：整批移到「已跳过」（不删，见 repo.skip_all）")
def clear_items(body: ClearIn):
    if body.status not in (R.PENDING, R.FAILED):
        raise HTTPException(400, "只能清空待下载或失败")
    return {"n": get_media_repo().skip_all(body.status)}


@router.post("/items/requeue-failed")
def requeue_failed():
    return {"n": get_media_repo().requeue_failed()}


# ── 运行 ─────────────────────────────────────────────────────────────────

class ScanIn(BaseModel):
    source_ids: Optional[List[str]] = None


def _launch(fn, *args):
    _guard_online()
    try:
        svc.run_in_background(fn, *args)
    except svc.Busy as e:
        raise HTTPException(409, detail={"busy": True, "msg": str(e)})
    return {"ok": True}


@router.post("/scan")
def scan(body: ScanIn = ScanIn()):
    return _launch(svc.scan, "manual", body.source_ids)


@router.post("/download")
def download(body: IdsIn = IdsIn()):
    st = agent.ping()
    free = st.get("free_gb")
    if free is not None and free < settings.media_min_free_gb:
        raise HTTPException(409, detail={"msg": f"D 盘只剩 {free} GB，先腾点地方"})
    if not get_media_repo().pending_items(body.ids or None):
        raise HTTPException(400, "待下载列表是空的")
    return _launch(svc.download, body.ids or None, "manual")


@router.post("/stop")
def stop():
    return svc.stop()


@router.post("/update-tools", summary="升级那台电脑上的 yt-dlp")
def update_tools():
    _guard_online()
    try:
        return svc.update_tools()
    except svc.Busy as e:
        raise HTTPException(409, detail={"busy": True, "msg": str(e)})


@router.get("/runs")
def runs(limit: int = 15):
    return {"items": [_doc(r) for r in get_media_repo().recent_runs(max(1, min(limit, 50)))]}


@router.delete("/runs", summary="清空运行记录（进行中的保留）")
def clear_runs():
    return {"n": get_media_repo().clear_runs()}
