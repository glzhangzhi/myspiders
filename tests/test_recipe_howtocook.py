"""HowToCook 导入器的纯函数测试（不碰 DB / 网络）。

源仓库格式高度统一（370 份 100% 有四段），故重点测**合并与边界**：
1. 「必备原料」只有名字、「计算」只有用量 —— 必须按名字对齐合成一条
2. 用量串里的孤立右括号与 `*注：` 尾巴
3. 「操作」段的 ### 小节要并进步骤文本（否则上下文丢失）
4. 缩进的 `- 说明` 是上一步的补充，不能单独成步
"""


from app.features.recipe.howtocook import (
    _clean_amount,
    _parse_ingredients,
    _parse_steps,
    _split_sections,
    build_recipe,
)


class TestCleanAmount:
    def test_strips_unpaired_closing_paren(self):
        # 「青红椒（根据受辣程度选择, 0-30g）」等号切分后右边拖着孤立右括号
        assert _clean_amount("0-30g）") == "0-30g"

    def test_strips_note_suffix(self):
        assert _clean_amount("0-30g *注：不建议使用肉厚的菜椒") == "0-30g"

    def test_keeps_balanced_parens(self):
        assert _clean_amount("1 个（约 180g）") == "1 个（约 180g）"

    def test_keeps_formula(self):
        assert _clean_amount("1.5 个 * 份数，向上取整") == "1.5 个 * 份数，向上取整"


class TestParseIngredients:
    def test_merges_names_with_amounts(self):
        names = ["西红柿", "鸡蛋", "盐"]
        calc = ["- 西红柿 = 1 个（约 180g） * 份数", "- 鸡蛋 = 1.5 个 * 份数", "- 盐 = 2g"]
        out = _parse_ingredients([f"- {n}" for n in names], calc)
        got = {i.name: i.amount for i in out}
        assert got["西红柿"] == "1 个（约 180g） * 份数"
        assert got["鸡蛋"] == "1.5 个 * 份数"

    def test_optional_marker_goes_to_note(self):
        out = _parse_ingredients(["- 糖（可选）"], ["- 糖 = 0-2g * 份数"])
        assert out[0].name == "糖"
        assert out[0].note == "可选"
        assert out[0].amount == "0-2g * 份数"

    def test_amount_without_equals(self):
        out = _parse_ingredients(["- 小葱"], ["- 小葱 2 棵"])
        assert out[0].amount == "2 棵"

    def test_ingredient_without_amount_kept(self):
        out = _parse_ingredients(["- 食用油"], [])
        assert out[0].name == "食用油"
        assert out[0].amount == ""

    def test_calc_only_item_is_added(self):
        # 计算段里有、原料段漏写的，也要收进来
        out = _parse_ingredients(["- 盐"], ["- 盐 = 2g", "- 老抽 = 5ml"])
        assert "老抽" in {i.name for i in out}


class TestParseSteps:
    def test_section_prefix_kept(self):
        lines = ["### 配菜处理", "1. 青红椒切圈", "2. 蒜苗切段"]
        steps = _parse_steps(lines)
        assert steps[0].text.startswith("【配菜处理】")
        assert len(steps) == 2

    def test_indented_note_merged_into_previous(self):
        # 缩进说明不是独立操作，单独成步会让步骤号虚增
        lines = ["1. 锅烧热", "  - 这一步是为了去腥", "2. 下锅炒"]
        steps = _parse_steps(lines)
        assert len(steps) == 2
        assert "去腥" in steps[0].text
        assert steps[1].text == "下锅炒"

    def test_orders_are_sequential(self):
        lines = ["### A", "1. 一", "### B", "1. 二", "2. 三"]
        steps = _parse_steps(lines)
        assert [s.order for s in steps] == [1, 2, 3]

    def test_inline_image_stripped_from_step(self):
        # 回归：只跳过独占一行的图片不够，行内图片会被带进步骤正文，
        # 推到 Kindle 上就是一行没法执行的乱码
        steps = _parse_steps(["1. 盛盘 ![成品](./x.jpg)"])
        assert steps[0].text == "盛盘"

    def test_image_only_line_is_not_a_step(self):
        steps = _parse_steps(["1. 炒香", "2. ![预览图](./y.png)"])
        assert len(steps) == 1
        assert steps[0].text == "炒香"

    def test_indented_note_without_previous_step_ignored(self):
        # 不能崩：没有上一步时缩进说明无处可挂
        assert _parse_steps(["  - 孤立说明"]) != []


class TestSplitSections:
    def test_title_strips_suffix(self):
        title, _, _ = _split_sections("# 回锅肉的做法\n\n## 操作\n1. 炒\n")
        assert title == "回锅肉"

    def test_sections_collected(self):
        _, _, sec = _split_sections(
            "# 菜的做法\n\n简介\n\n## 必备原料和工具\n- 盐\n\n## 操作\n1. 炒\n"
        )
        assert "必备原料和工具" in sec and "操作" in sec

    def test_intro_captured(self):
        _, intro, _ = _split_sections("# 菜的做法\n\n这是简介\n\n## 操作\n1. 炒\n")
        assert "这是简介" in intro


class TestBuildRecipe:
    SAMPLE = """# 西红柿炒鸡蛋的做法

一道酸甜开胃的家常菜，大约只需 15 分钟。

预估烹饪难度：★★

预估卡路里：252 大卡

## 必备原料和工具

- 西红柿
- 鸡蛋
- 糖（可选）

## 计算

- 西红柿 = 1 个（约 180g） * 份数
- 鸡蛋 = 1.5 个 * 份数
- 糖 = 0-2g * 份数

## 操作

1. 西红柿洗净
2. 切块

  - 边长不超过 4cm

## 附加内容

- 可以加番茄酱增加汤汁
- 如果您遵循本指南的制作流程而发现有问题，请提出 Issue 或 Pull request 。
"""

    def test_parses_completely(self):
        r = build_recipe(self.SAMPLE, category="炒菜")
        assert r.title == "西红柿炒鸡蛋"
        assert r.parse_status == "parsed"
        assert len(r.ingredients) == 3
        assert len(r.steps) == 2

    def test_explicit_minutes_wins_over_stars(self):
        # 正文写了「约 15 分钟」，应优先于 ★★ 的默认 10 分钟
        assert build_recipe(self.SAMPLE).total_minutes == 15

    def test_star_fallback_when_no_explicit_time(self):
        text = self.SAMPLE.replace("大约只需 15 分钟", "很好吃")
        assert build_recipe(text).total_minutes == 10   # ★★ → 10

    def test_tips_include_difficulty_and_calories(self):
        r = build_recipe(self.SAMPLE)
        assert "★★" in r.tips
        assert "252" in r.tips

    def test_tips_drop_contribution_boilerplate(self):
        # 每篇末尾的"请提 Issue"对做饭没用
        r = build_recipe(self.SAMPLE)
        assert "Issue" not in r.tips
        assert "番茄酱" in r.tips

    def test_slug_uses_howtocook_source(self):
        assert build_recipe(self.SAMPLE).slug == "howtocook:西红柿炒鸡蛋"

    def test_html_comments_stripped(self):
        text = "<!-- 注释 -->\n" + self.SAMPLE
        assert "注释" not in build_recipe(text).raw_text
