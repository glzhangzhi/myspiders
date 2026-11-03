"""四柱干支与节气计算 (基于 sxtwl 寿星天文历)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

import sxtwl

from .constants import DI_ZHI, JIE_QI_NAMES, TIAN_GAN


@dataclass(frozen=True, slots=True)
class GanZhi:
    """一组天干地支。"""

    tg: int  # 天干索引 (0=甲 ... 9=癸)
    dz: int  # 地支索引 (0=子 ... 11=亥)

    def __str__(self) -> str:
        return f"{TIAN_GAN[self.tg]}{DI_ZHI[self.dz]}"


@dataclass(frozen=True, slots=True)
class SiZhu:
    """四柱 (年月日时干支)。"""

    year: GanZhi
    month: GanZhi
    day: GanZhi
    hour: GanZhi

    def __str__(self) -> str:
        return f"{self.year} {self.month} {self.day} {self.hour}"


@dataclass(frozen=True, slots=True)
class JieQiInfo:
    """当前所处节气的详细信息。"""

    name: str           # 节气名称
    start_date: date    # 该节气的起始日期
    days_elapsed: int   # 从节气开始到当前日的天数


@dataclass(frozen=True, slots=True)
class CalendarResult:
    """日历计算的完整结果。"""

    si_zhu: SiZhu
    jie_qi: JieQiInfo
    lunar_month: int
    lunar_day: int
    is_leap_month: bool


def _get_hour_gz(day_tg: int, hour: int) -> GanZhi:
    """根据日干和小时计算时干支 (五鼠遁)。"""
    zhi_idx = ((hour + 1) % 24) // 2
    # 五鼠遁: 甲己起甲子, 乙庚起丙子, 丙辛起戊子, 丁壬起庚子, 戊癸起壬子
    hour_tg_base = [0, 2, 4, 6, 8]
    hour_tg = (hour_tg_base[day_tg % 5] + zhi_idx) % 10
    return GanZhi(tg=hour_tg, dz=zhi_idx)


def _collect_jie_qi_dates(year: int) -> List[Tuple[str, date]]:
    """收集指定年份及前一年末的所有节气日期。

    返回按日期升序排列的 (节气名, 日期) 列表。
    """
    results: List[Tuple[str, date]] = []
    for y in (year - 1, year, year + 1):
        for m in range(1, 13):
            for d in range(1, 32):
                try:
                    day_obj = sxtwl.fromSolar(y, m, d)
                except Exception:
                    continue
                if day_obj.hasJieQi():
                    jq_idx = day_obj.getJieQi()
                    jq_name = JIE_QI_NAMES[jq_idx]
                    results.append((jq_name, date(y, m, d)))
    results.sort(key=lambda x: x[1])
    return results


def _find_current_jie_qi(
    target: date, jie_qi_dates: List[Tuple[str, date]]
) -> JieQiInfo:
    """找到 target 所处的节气 (最近的、不晚于 target 的节气)。"""
    current_jq: Optional[Tuple[str, date]] = None
    for name, dt in jie_qi_dates:
        if dt <= target:
            current_jq = (name, dt)
        else:
            break
    if current_jq is None:
        raise ValueError(f"无法找到 {target} 对应的节气")
    name, start = current_jq
    days_elapsed = (target - start).days
    return JieQiInfo(name=name, start_date=start, days_elapsed=days_elapsed)


def calculate_calendar(dt: datetime) -> CalendarResult:
    """计算指定时间的四柱干支、节气和农历信息。

    注意: 23:00 以后算次日子时，但日柱不变 (传统以 23:00 换日的流派)。
    这里采用「晚子时仍归本日」的做法，即日柱不因 23:00 而进位。
    """
    year, month, day_num, hour = dt.year, dt.month, dt.day, dt.hour

    day_obj = sxtwl.fromSolar(year, month, day_num)

    year_gz = GanZhi(tg=day_obj.getYearGZ().tg, dz=day_obj.getYearGZ().dz)
    month_gz = GanZhi(tg=day_obj.getMonthGZ().tg, dz=day_obj.getMonthGZ().dz)
    day_gz = GanZhi(tg=day_obj.getDayGZ().tg, dz=day_obj.getDayGZ().dz)
    hour_gz = _get_hour_gz(day_gz.tg, hour)

    si_zhu = SiZhu(year=year_gz, month=month_gz, day=day_gz, hour=hour_gz)

    jie_qi_dates = _collect_jie_qi_dates(year)
    jie_qi = _find_current_jie_qi(date(year, month, day_num), jie_qi_dates)

    lunar_month = day_obj.getLunarMonth()
    lunar_day = day_obj.getLunarDay()
    is_leap = day_obj.isLunarLeap()

    return CalendarResult(
        si_zhu=si_zhu,
        jie_qi=jie_qi,
        lunar_month=lunar_month,
        lunar_day=lunar_day,
        is_leap_month=is_leap,
    )
