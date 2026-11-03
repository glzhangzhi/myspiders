"""供 API 层复用的响应模型。"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel


class QiMenPalace(BaseModel):
    """单宫内容。"""

    palace_num: int
    di_pan_stem: str
    tian_pan_stem: str
    star: str
    gate: str
    spirit: str


class QiMenResponse(BaseModel):
    """奇门遁甲排盘响应。"""

    # 基础
    datetime_str: str
    year_gz: str
    month_gz: str
    day_gz: str
    hour_gz: str
    si_zhu: str
    jie_qi: str
    jie_qi_start: str
    days_in_jie_qi: int

    # 遁甲
    dun_type: str
    yuan: str
    ju_num: int

    # 旬首
    xun_shou: str
    xun_yi: str
    xun_kong: List[str]

    # 值符值使
    zhi_fu_xing: str
    zhi_shi_men: str

    # 各层布局 (宫号字符串 → 内容)
    di_pan: Dict[str, str]
    tian_pan: Dict[str, str]
    jiu_xing: Dict[str, str]
    ba_men: Dict[str, str]
    ba_shen: Dict[str, str]

    # 九宫
    palaces: Dict[str, QiMenPalace]

    # 逐步文本
    formatted_text: str
