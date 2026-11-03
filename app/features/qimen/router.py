"""奇门遁甲排盘 API（原 /utils/qimen，2026-11 随掷笅占卜退役迁到 /api/qimen）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.features.qimen.models import QiMenPalace, QiMenResponse
from app.features.qimen.pan.engine import generate_pan
from app.features.qimen.pan.formatter import format_step_by_step

router = APIRouter(prefix="/api/qimen", tags=["qimen"])


class QiMenRequest(BaseModel):
    """奇门遁甲排盘请求。"""

    datetime_str: str = Field(
        default="",
        description="排盘时间，格式 'YYYY-MM-DD HH:MM'，留空则用当前时间",
    )


@router.post("", summary="奇门遁甲排盘", response_model=QiMenResponse)
def qimen_pan(payload: QiMenRequest) -> QiMenResponse:
    """输入时间（或使用当前时间），返回奇门遁甲时家排盘。"""
    raw = payload.datetime_str.strip()
    if raw:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail="时间格式错误，请使用 'YYYY-MM-DD HH:MM'")
    else:
        dt = datetime.now()

    try:
        pan = generate_pan(dt)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"排盘失败: {exc}")

    return QiMenResponse(
        datetime_str=pan.datetime_str,
        year_gz=pan.year_gz,
        month_gz=pan.month_gz,
        day_gz=pan.day_gz,
        hour_gz=pan.hour_gz,
        si_zhu=pan.si_zhu_str,
        jie_qi=pan.jie_qi_name,
        jie_qi_start=pan.jie_qi_start,
        days_in_jie_qi=pan.days_in_jie_qi,
        dun_type=pan.dun_type,
        yuan=pan.yuan,
        ju_num=pan.ju_num,
        xun_shou=pan.xun_shou,
        xun_yi=pan.xun_yi,
        xun_kong=list(pan.xun_kong),
        zhi_fu_xing=pan.zhi_fu_xing,
        zhi_shi_men=pan.zhi_shi_men,
        di_pan={str(k): v for k, v in pan.di_pan.items()},
        tian_pan={str(k): v for k, v in pan.tian_pan.items()},
        jiu_xing={str(k): v for k, v in pan.jiu_xing.items()},
        ba_men={str(k): v for k, v in pan.ba_men.items()},
        ba_shen={str(k): v for k, v in pan.ba_shen.items()},
        palaces={
            str(k): QiMenPalace(
                palace_num=v.palace_num,
                di_pan_stem=v.di_pan_stem,
                tian_pan_stem=v.tian_pan_stem,
                star=v.star,
                gate=v.gate,
                spirit=v.spirit,
            )
            for k, v in pan.palaces.items()
        },
        formatted_text=format_step_by_step(pan),
    )
