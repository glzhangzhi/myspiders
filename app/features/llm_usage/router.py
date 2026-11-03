"""统一 LLM 用量 API —— 供仪表盘「LLM 用量」页（取代原梨园用量页）。

「当月」按**德国时间**的自然月划界（机主人在德国）；计价的峰谷判定则按每条调用
自己的 **UTC** 时刻落到 DeepSeek 的窗口 —— 展示分桶与计费窗口是两件独立的事。
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import require_admin_token
from app.features.llm_usage.repo import llm_usage_repo
from app.features.llm_usage.liyuan_billing import next_window_change
from app.features.llm_usage.pricing import TARGETS, summarize, summarize_daily
from app.features.llm_usage.projects import load_machines, summarize_projects
from app.features.llm_usage.service import ingest

router = APIRouter(prefix="/api/llm-usage", tags=["LLM 用量"])

DISPLAY_TZ = "Europe/Berlin"
TZ_DE = ZoneInfo(DISPLAY_TZ)
_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def month_range(month: str | None, now: dt.datetime | None = None) -> tuple[str, dt.datetime, dt.datetime]:
    """`YYYY-MM`（缺省 = 德国时间的本月）→ (月份, UTC 起, UTC 止)，左闭右开。"""
    if month is None:
        now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(TZ_DE)
        month = f"{now:%Y-%m}"
    m = _MONTH_RE.match(month)
    if not m:
        raise ValueError(f"月份格式应为 YYYY-MM：{month}")
    y, mo = int(m.group(1)), int(m.group(2))
    start = dt.datetime(y, mo, 1, tzinfo=TZ_DE)
    end = dt.datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=TZ_DE)
    return month, start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


@router.get("/summary", summary="某月花费总计、来源拆分、命中/未命中/输出构成")
def get_summary(
    month: str | None = Query(None, description="YYYY-MM，德国时间自然月；缺省为本月"),
    target: str = Query("deepseek-flash", description="非 DeepSeek 调用按哪个模型估价"),
    force: bool = Query(False, description="true = 所有调用都按 target 计价"),
):
    if target not in TARGETS:
        raise HTTPException(400, f"target 只能是 {', '.join(TARGETS)}")
    try:
        month, start, end = month_range(month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    data = summarize(llm_usage_repo.docs_between(start, end), target, force)
    now = dt.datetime.now(dt.timezone.utc)
    peak_now, switch_at = next_window_change(now)
    data.update({
        "month": month,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "last_seen": llm_usage_repo.last_seen(),
        "window": {
            "peak_now": peak_now,
            "switch_at": switch_at.isoformat(),
            "minutes_left": int((switch_at - now).total_seconds() // 60),
        },
    })
    return data


@router.get("/projects", summary="DSH 花费按 大类 → 项目 汇总（项目 = 工作目录最后一级文件夹）")
def get_projects(
    month: str | None = Query(None, description="YYYY-MM，德国时间自然月；缺省为本月；all = 全部"),
    target: str = Query("deepseek-flash"),
    force: bool = Query(False),
):
    if target not in TARGETS:
        raise HTTPException(400, f"target 只能是 {', '.join(TARGETS)}")
    if month == "all":
        start = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2100, 1, 1, tzinfo=dt.timezone.utc)
    else:
        try:
            month, start, end = month_range(month)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    categories = summarize_projects(
        llm_usage_repo.docs_between(start, end), llm_usage_repo.session_cwds(),
        target, force, machines=load_machines(),
    )
    return {"month": month, "target": target, "force": force, "categories": categories}


@router.get("/daily", summary="某月每天的花费（德国时间自然日，没调用的日子也给 0）")
def get_daily(
    month: str | None = Query(None, description="YYYY-MM，德国时间自然月；缺省为本月"),
    target: str = Query("deepseek-flash"),
    force: bool = Query(False),
):
    if target not in TARGETS:
        raise HTTPException(400, f"target 只能是 {', '.join(TARGETS)}")
    try:
        month, start, end = month_range(month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    first = start.astimezone(TZ_DE).date()
    last = (end.astimezone(TZ_DE) - dt.timedelta(days=1)).date()
    # 本月只画到今天：未来的日子画成 0 会和「那天没用」混在一起
    today = dt.datetime.now(TZ_DE).date()
    last = min(last, max(first, today))
    days = summarize_daily(llm_usage_repo.docs_between(start, end), target, force, first, last, TZ_DE)
    return {"month": month, "target": target, "force": force, "days": days}


@router.get("/months", summary="有数据的月份（新的在前）")
def get_months():
    return {"months": llm_usage_repo.months(DISPLAY_TZ)}


@router.post("/ingest", summary="立即扫描三个来源并导入", dependencies=[Depends(require_admin_token)])
def post_ingest():
    return ingest().as_dict()
