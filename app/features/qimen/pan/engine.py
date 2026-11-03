"""奇门遁甲核心排盘引擎。

采用时家奇门「拆补法」排盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Tuple

from .calendar import GanZhi, calculate_calendar
from .constants import (
    BA_MEN,
    BA_SHEN_YIN,
    BA_SHEN_YANG,
    JIU_XING,
    JU_TABLE_YANG,
    JU_TABLE_YIN,
    PALACE_ORDER,
    SAN_QI_LIU_YI,
    TIAN_GAN,
    XUN_KONG,
    XUN_SHOU_MAP,
    YANG_DUN_JIE_QI,
)


# ── 数据结构 ──


@dataclass(frozen=True, slots=True)
class PalaceContent:
    """单个宫位的内容。"""

    palace_num: int
    di_pan_stem: str       # 地盘天干
    tian_pan_stem: str     # 天盘天干
    star: str              # 九星
    gate: str              # 八门
    spirit: str            # 八神


@dataclass(slots=True)
class QiMenPan:
    """完整的奇门遁甲盘面。"""

    # 基础信息
    datetime_str: str
    si_zhu_str: str
    year_gz: str
    month_gz: str
    day_gz: str
    hour_gz: str
    jie_qi_name: str
    jie_qi_start: str
    days_in_jie_qi: int

    # 遁甲信息
    dun_type: str          # "阳遁" / "阴遁"
    yuan: str              # "上元" / "中元" / "下元"
    ju_num: int            # 局数 1-9

    # 旬首信息
    xun_shou: str          # 旬首名 (如 "甲子")
    xun_yi: str            # 旬仪 (如 "戊")
    xun_kong: Tuple[str, str]  # 旬空

    # 值符值使
    zhi_fu_xing: str       # 值符星
    zhi_shi_men: str       # 值使门

    # 盘面布局 (宫号 → 内容)
    di_pan: Dict[int, str]     # 地盘三奇六仪
    tian_pan: Dict[int, str]   # 天盘三奇六仪
    jiu_xing: Dict[int, str]   # 九星分布
    ba_men: Dict[int, str]     # 八门分布
    ba_shen: Dict[int, str]    # 八神分布

    # 九宫完整内容
    palaces: Dict[int, PalaceContent] = field(default_factory=dict)


# ── 核心计算函数 ──


def _determine_dun_type(jie_qi_name: str) -> str:
    """判断阳遁还是阴遁。"""
    if jie_qi_name in YANG_DUN_JIE_QI:
        return "阳遁"
    return "阴遁"


def _determine_yuan(days_elapsed: int) -> str:
    """根据节气内天数判断上中下元 (拆补法)。"""
    if days_elapsed < 5:
        return "上元"
    elif days_elapsed < 10:
        return "中元"
    else:
        return "下元"


def _get_yuan_index(yuan: str) -> int:
    """上元=0, 中元=1, 下元=2。"""
    return {"上元": 0, "中元": 1, "下元": 2}[yuan]


def _lookup_ju(jie_qi_name: str, yuan: str) -> int:
    """查表获取局数。"""
    yuan_idx = _get_yuan_index(yuan)
    if jie_qi_name in JU_TABLE_YANG:
        return JU_TABLE_YANG[jie_qi_name][yuan_idx]
    return JU_TABLE_YIN[jie_qi_name][yuan_idx]


def _layout_di_pan(ju_num: int, is_yang: bool) -> Dict[int, str]:
    """排布地盘三奇六仪。

    阳遁: 从 ju_num 宫起，按 1→2→3→4→5→6→7→8→9 顺排
    阴遁: 从 ju_num 宫起，按 9→8→7→6→5→4→3→2→1 逆排
    """
    di_pan: Dict[int, str] = {}
    for i, stem in enumerate(SAN_QI_LIU_YI):
        if is_yang:
            palace = (ju_num - 1 + i) % 9 + 1
        else:
            palace = (ju_num - 1 - i) % 9 + 1
        di_pan[palace] = stem
    return di_pan


def _find_xun_info(gz: GanZhi) -> Tuple[str, int, str, Tuple[str, str]]:
    """根据干支找旬首信息。

    返回: (旬首名, 旬仪在三奇六仪中的位置, 旬仪名, 旬空)
    """
    diff = (gz.dz - gz.tg) % 12
    xun_name, yi_pos = XUN_SHOU_MAP[diff]
    yi_name = SAN_QI_LIU_YI[yi_pos]
    kong = XUN_KONG[xun_name]
    return xun_name, yi_pos, yi_name, kong


def _find_stem_palace(di_pan: Dict[int, str], stem: str) -> int:
    """在地盘上找某个天干所在的宫位。"""
    for palace, s in di_pan.items():
        if s == stem:
            return palace
    raise ValueError(f"地盘上找不到天干: {stem}")


def _map_hour_stem_to_di_pan_stem(hour_gz: GanZhi, xun_yi_name: str) -> str:
    """将时干映射到地盘上的三奇六仪。

    甲遁于旬仪中，故时干为甲时，用旬仪代替。
    其余天干直接在地盘上找对应位置。
    """
    hour_tg = hour_gz.tg
    if hour_tg == 0:  # 甲
        return xun_yi_name
    stem_name = TIAN_GAN[hour_tg]
    if stem_name in SAN_QI_LIU_YI:
        return stem_name
    raise ValueError(f"时干 {stem_name} 不在三奇六仪中")


def _palace_index(palace: int) -> int:
    """宫号在 PALACE_ORDER (8宫序列) 中的索引。"""
    return PALACE_ORDER.index(palace)


def _palace_at_index(idx: int) -> int:
    """PALACE_ORDER 中某索引对应的宫号。"""
    return PALACE_ORDER[idx % 8]


def _rotate_layer(
    original: Dict[int, str],
    shift: int,
) -> Dict[int, str]:
    """将一个八宫层 (九星/八门/天盘) 整体旋转 shift 步。

    shift > 0 表示顺时针 (正向), shift < 0 表示逆时针 (反向)。
    宫5的内容寄于宫2，不参与旋转。
    """
    result: Dict[int, str] = {}
    for palace, content in original.items():
        if palace == 5:
            continue
        old_idx = _palace_index(palace)
        new_idx = (old_idx + shift) % 8
        result[_palace_at_index(new_idx)] = content
    # 处理宫5内容 (寄坤2宫)
    if 5 in original:
        result[5] = original[5]
    return result


def _layout_tian_pan(
    di_pan: Dict[int, str],
    xun_yi_palace: int,
    shi_gan_palace: int,
) -> Dict[int, str]:
    """排布天盘。

    值符 (旬仪) 从地盘原宫飞到时干所在宫，其余保持相对位置。
    """
    # 宫5寄宫2处理
    src = xun_yi_palace if xun_yi_palace != 5 else 2
    dst = shi_gan_palace if shi_gan_palace != 5 else 2

    shift = (_palace_index(dst) - _palace_index(src)) % 8
    return _rotate_layer(di_pan, shift)


def _layout_jiu_xing(
    xun_yi_palace: int,
    shi_gan_palace: int,
) -> Dict[int, str]:
    """排布九星。

    值符星 (旬仪所在宫的星) 飞到时干所在宫，其余保持相对位置。
    """
    src = xun_yi_palace if xun_yi_palace != 5 else 2
    dst = shi_gan_palace if shi_gan_palace != 5 else 2
    shift = (_palace_index(dst) - _palace_index(src)) % 8

    original = {p: JIU_XING[p] for p in PALACE_ORDER}
    original[5] = JIU_XING[5]  # 天禽寄坤
    return _rotate_layer(original, shift)


def _layout_ba_men(
    xun_yi_palace: int,
    hour_tg: int,
    is_yang: bool,
) -> Dict[int, str]:
    """排布八门。

    值使门从旬仪所在宫起，按时干偏移步数转动。
    阳遁顺转，阴遁逆转。
    """
    src = xun_yi_palace if xun_yi_palace != 5 else 2

    offset = hour_tg  # 甲=0, 乙=1, ..., 癸=9
    if is_yang:
        shift = offset
    else:
        shift = -offset

    original = {p: BA_MEN[p] for p in PALACE_ORDER}
    return _rotate_layer(original, shift)


def _layout_ba_shen(
    zhi_fu_palace: int,
    is_yang: bool,
) -> Dict[int, str]:
    """排布八神。

    从值符星所在宫起，阳遁顺排，阴遁逆排。
    """
    shen_list = BA_SHEN_YANG if is_yang else BA_SHEN_YIN
    start_idx = _palace_index(zhi_fu_palace)

    result: Dict[int, str] = {}
    for i, shen in enumerate(shen_list):
        if is_yang:
            idx = (start_idx + i) % 8
        else:
            idx = (start_idx - i) % 8
        result[_palace_at_index(idx)] = shen
    return result


# ── 主排盘函数 ──


def generate_pan(dt: datetime) -> QiMenPan:
    """给定时间，生成完整的奇门遁甲盘。"""

    cal = calculate_calendar(dt)
    sz = cal.si_zhu
    jq = cal.jie_qi

    # 1. 阴阳遁
    dun_type = _determine_dun_type(jq.name)
    is_yang = dun_type == "阳遁"

    # 2. 三元
    yuan = _determine_yuan(jq.days_elapsed)

    # 3. 局数
    ju_num = _lookup_ju(jq.name, yuan)

    # 4. 地盘
    di_pan = _layout_di_pan(ju_num, is_yang)

    # 5. 旬首信息 (基于时柱)
    xun_name, xun_yi_pos, xun_yi_name, xun_kong = _find_xun_info(sz.hour)

    # 6. 找旬仪在地盘上的宫位
    xun_yi_palace = _find_stem_palace(di_pan, xun_yi_name)

    # 7. 找时干在地盘上的宫位
    shi_gan_stem = _map_hour_stem_to_di_pan_stem(sz.hour, xun_yi_name)
    shi_gan_palace = _find_stem_palace(di_pan, shi_gan_stem)

    # 8. 值符星、值使门
    zhi_fu_xing_palace = xun_yi_palace if xun_yi_palace != 5 else 2
    zhi_fu_xing = JIU_XING[xun_yi_palace]
    zhi_shi_palace = xun_yi_palace if xun_yi_palace != 5 else 2
    zhi_shi_men = BA_MEN.get(zhi_shi_palace, BA_MEN[2])

    # 9. 天盘
    tian_pan = _layout_tian_pan(di_pan, xun_yi_palace, shi_gan_palace)

    # 10. 九星
    jiu_xing = _layout_jiu_xing(xun_yi_palace, shi_gan_palace)

    # 11. 八门
    ba_men = _layout_ba_men(xun_yi_palace, sz.hour.tg, is_yang)

    # 12. 八神 (从值符星的新位置起排)
    zhi_fu_new_palace = shi_gan_palace if shi_gan_palace != 5 else 2
    ba_shen = _layout_ba_shen(zhi_fu_new_palace, is_yang)

    # 13. 组装九宫内容
    palaces: Dict[int, PalaceContent] = {}
    for p in PALACE_ORDER:
        palaces[p] = PalaceContent(
            palace_num=p,
            di_pan_stem=di_pan.get(p, di_pan.get(5, "")),
            tian_pan_stem=tian_pan.get(p, ""),
            star=jiu_xing.get(p, ""),
            gate=ba_men.get(p, ""),
            spirit=ba_shen.get(p, ""),
        )

    return QiMenPan(
        datetime_str=dt.strftime("%Y年%m月%d日 %H:%M"),
        si_zhu_str=str(sz),
        year_gz=str(sz.year),
        month_gz=str(sz.month),
        day_gz=str(sz.day),
        hour_gz=str(sz.hour),
        jie_qi_name=jq.name,
        jie_qi_start=jq.start_date.isoformat(),
        days_in_jie_qi=jq.days_elapsed,
        dun_type=dun_type,
        yuan=yuan,
        ju_num=ju_num,
        xun_shou=xun_name,
        xun_yi=xun_yi_name,
        xun_kong=xun_kong,
        zhi_fu_xing=zhi_fu_xing,
        zhi_shi_men=zhi_shi_men,
        di_pan=di_pan,
        tian_pan=tian_pan,
        jiu_xing=jiu_xing,
        ba_men=ba_men,
        ba_shen=ba_shen,
        palaces=palaces,
    )
