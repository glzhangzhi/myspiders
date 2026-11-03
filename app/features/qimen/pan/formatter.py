"""奇门遁甲盘面文本格式化，逐步展示排盘过程。"""

from __future__ import annotations

from .constants import PALACE_DIRECTIONS, PALACE_WUXING
from .engine import QiMenPan


def format_step_by_step(pan: QiMenPan) -> str:
    """将排盘结果格式化为逐步展示的文本。"""
    lines: list[str] = []

    # ═══ 第一步: 四柱与节气 ═══
    lines.append("═" * 50)
    lines.append("【第一步】四柱干支与节气")
    lines.append("═" * 50)
    lines.append(f"  排盘时间: {pan.datetime_str}")
    lines.append(f"  年柱: {pan.year_gz}")
    lines.append(f"  月柱: {pan.month_gz}")
    lines.append(f"  日柱: {pan.day_gz}")
    lines.append(f"  时柱: {pan.hour_gz}")
    lines.append(f"  四柱: {pan.si_zhu_str}")
    lines.append(f"  节气: {pan.jie_qi_name} (起始: {pan.jie_qi_start})")
    lines.append(f"  节气内第 {pan.days_in_jie_qi + 1} 天")
    lines.append("")

    # ═══ 第二步: 阴阳遁 ═══
    lines.append("═" * 50)
    lines.append("【第二步】阴阳遁判断")
    lines.append("═" * 50)
    if pan.dun_type == "阳遁":
        lines.append(f"  当前节气「{pan.jie_qi_name}」处于冬至→夏至区间")
    else:
        lines.append(f"  当前节气「{pan.jie_qi_name}」处于夏至→冬至区间")
    lines.append(f"  → {pan.dun_type}")
    lines.append("")

    # ═══ 第三步: 局数 ═══
    lines.append("═" * 50)
    lines.append("【第三步】三元与局数")
    lines.append("═" * 50)
    lines.append(f"  节气「{pan.jie_qi_name}」内第 {pan.days_in_jie_qi + 1} 天")
    lines.append(f"  三元: {pan.yuan}")
    lines.append(f"  → {pan.dun_type}{pan.ju_num}局")
    lines.append(f"  旬首: {pan.xun_shou}旬 (遁甲仪={pan.xun_yi})")
    lines.append(f"  旬空: {pan.xun_kong[0]}、{pan.xun_kong[1]}")
    lines.append(f"  值符星: {pan.zhi_fu_xing}")
    lines.append(f"  值使门: {pan.zhi_shi_men}")
    lines.append("")

    # ═══ 第四步: 地盘 ═══
    lines.append("═" * 50)
    lines.append("【第四步】地盘三奇六仪排布")
    lines.append("═" * 50)
    lines.append(f"  排法: {pan.dun_type} → 从{pan.ju_num}宫起，"
                 f"{'顺排' if pan.dun_type == '阳遁' else '逆排'}戊己庚辛壬癸丁丙乙")
    lines.append("")
    _append_single_layer_grid(lines, pan.di_pan, "地盘")
    lines.append("")

    # ═══ 第五步: 天盘 ═══
    lines.append("═" * 50)
    lines.append("【第五步】天盘排布")
    lines.append("═" * 50)
    lines.append(f"  值符(旬仪{pan.xun_yi})随时干转动")
    lines.append("")
    _append_single_layer_grid(lines, pan.tian_pan, "天盘")
    lines.append("")

    # ═══ 第六步: 九星 ═══
    lines.append("═" * 50)
    lines.append("【第六步】九星排布")
    lines.append("═" * 50)
    lines.append(f"  值符星「{pan.zhi_fu_xing}」随时干转动")
    lines.append("")
    _append_single_layer_grid(lines, pan.jiu_xing, "九星")
    lines.append("")

    # ═══ 第七步: 八门 ═══
    lines.append("═" * 50)
    lines.append("【第七步】八门排布")
    lines.append("═" * 50)
    lines.append(f"  值使门「{pan.zhi_shi_men}」按时干偏移"
                 f"{'顺转' if pan.dun_type == '阳遁' else '逆转'}")
    lines.append("")
    _append_single_layer_grid(lines, pan.ba_men, "八门")
    lines.append("")

    # ═══ 第八步: 八神 ═══
    lines.append("═" * 50)
    lines.append("【第八步】八神排布")
    lines.append("═" * 50)
    shen_type = "阳遁八神" if pan.dun_type == "阳遁" else "阴遁八神"
    lines.append(f"  {shen_type}，从值符星所在宫起"
                 f"{'顺排' if pan.dun_type == '阳遁' else '逆排'}")
    lines.append("")
    _append_single_layer_grid(lines, pan.ba_shen, "八神")
    lines.append("")

    # ═══ 总盘 ═══
    lines.append("═" * 50)
    lines.append(f"【总盘】{pan.dun_type}{pan.ju_num}局 {pan.yuan} "
                 f"{pan.si_zhu_str} ({pan.jie_qi_name})")
    lines.append("═" * 50)
    _append_full_pan(lines, pan)

    return "\n".join(lines)


def _append_single_layer_grid(
    lines: list[str],
    layer: dict[int, str],
    label: str,
) -> None:
    """以九宫格形式展示单层内容。"""
    # 九宫格布局:
    #   巽4  | 离9  | 坤2
    #   震3  | 中5  | 兑7
    #   艮8  | 坎1  | 乾6
    grid = [
        [4, 9, 2],
        [3, 5, 7],
        [8, 1, 6],
    ]

    for row in grid:
        cells = []
        for p in row:
            direction = PALACE_DIRECTIONS[p]
            content = layer.get(p, "")
            if p == 5:
                content = layer.get(5, layer.get(2, "")) + "(寄2)"
            cells.append(f" {direction}{p}:{content:　<4}")
        lines.append("  │".join(cells))
    lines.append("")


def _append_full_pan(lines: list[str], pan: QiMenPan) -> None:
    """以九宫格形式展示完整盘面。"""
    grid = [
        [4, 9, 2],
        [3, 5, 7],
        [8, 1, 6],
    ]

    divider = "  ┌" + "┬".join(["─" * 18] * 3) + "┐"
    mid_divider = "  ├" + "┼".join(["─" * 18] * 3) + "┤"
    end_divider = "  └" + "┴".join(["─" * 18] * 3) + "┘"

    lines.append(divider)
    for ri, row in enumerate(grid):
        # 第1行: 八神 / 九星
        cells_1 = []
        for p in row:
            if p == 5:
                cells_1.append(f"   {'中五宫':　<8}")
            else:
                spirit = pan.ba_shen.get(p, "")
                star = pan.jiu_xing.get(p, "")
                cells_1.append(f" {spirit:　<4} {star:　<4}")
        lines.append("  │" + "│".join(cells_1) + "│")

        # 第2行: 天盘干 / 八门
        cells_2 = []
        for p in row:
            if p == 5:
                tian = pan.tian_pan.get(5, pan.tian_pan.get(2, ""))
                di = pan.di_pan.get(5, pan.di_pan.get(2, ""))
                cells_2.append(f"  天{tian} 地{di} (寄2) ")
            else:
                tian = pan.tian_pan.get(p, "")
                gate = pan.ba_men.get(p, "")
                cells_2.append(f" 天{tian:　<4} {gate:　<4}")
        lines.append("  │" + "│".join(cells_2) + "│")

        # 第3行: 地盘干 / 方位
        cells_3 = []
        for p in row:
            if p == 5:
                cells_3.append(f"   {PALACE_WUXING[p]}{'':　<7}")
            else:
                di = pan.di_pan.get(p, "")
                direction = PALACE_DIRECTIONS[p]
                cells_3.append(f" 地{di:　<4} {direction:　<4}")
        lines.append("  │" + "│".join(cells_3) + "│")

        if ri < 2:
            lines.append(mid_divider)
    lines.append(end_divider)
