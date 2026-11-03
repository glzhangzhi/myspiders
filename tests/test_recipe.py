"""菜谱模块的纯函数测试（不碰 DB / 网络）。

覆盖真出过问题或最易回归的点：
1. 配料行解析 —— 嵌套括号曾把「小炒肉调料（聚慧（重庆）、烟台欣和）」拆错
2. 标签派生 —— 「拌匀」曾让粉蒸肉被打上「凉拌」；「鱼香」不该命中「鱼虾」
3. Markdown 章节变体 —— 源仓库有 `## 原料：` / `## 步骤:` 等多种写法
4. Kindle 渲染 —— 步骤自带的 "1. " 前缀会导致 "[1] 1. xxx" 重复编号；文件名按菜名覆盖
5. AI 返回值清洗 —— 模型会自创标签、返回纯字符串数组
"""


import pytest

from app.features.recipe.convert import (
    _extract_json,
    _sanitize_tags,
    build_recipe_from_payload,
    extract_questions,
    render_doc_for_convert,
)
from app.features.recipe.hoc_import import (
    _split_parens,
    build_recipe,
    parse_ingredient_line,
    parse_markdown,
)
from app.features.recipe.kindle import (
    _safe_filename,
    recipe_filename,
    render_recipe,
    render_recipe_file,
)
from app.features.recipe.tags import FLAVOR_TAGS, METHOD_TAGS, derive_tags, make_slug


class TestSplitParens:
    """源数据里括注套括注很常见，非贪婪正则会在第一个右括号处收尾。"""

    def test_nested_parens(self):
        notes, text = _split_parens("小炒肉调料（聚慧（重庆）、烟台欣和）")
        assert text == "小炒肉调料"
        assert notes == ["聚慧（重庆）、烟台欣和"]

    def test_multiple_groups(self):
        notes, text = _split_parens("东方树叶（335mL）（农夫山泉（淳安坪山））")
        assert text == "东方树叶"
        assert notes == ["335mL", "农夫山泉（淳安坪山）"]

    def test_no_parens(self):
        notes, text = _split_parens("五花肉片")
        assert (notes, text) == ([], "五花肉片")

    def test_unclosed_paren_keeps_content(self):
        notes, text = _split_parens("豆瓣酱（郫县")
        assert "郫县" in text


class TestParseIngredientLine:
    def test_amount_prefix(self):
        ing = parse_ingredient_line("30g 米粉")
        assert ing.name == "米粉"
        assert ing.amount == "30g"

    def test_amount_with_unit_no_space(self):
        ing = parse_ingredient_line("2勺生抽（海天）")
        assert ing.name == "生抽"
        assert ing.amount == "2勺"
        assert ing.note == "海天"

    def test_bare_name(self):
        ing = parse_ingredient_line("盐")
        assert ing.name == "盐"
        assert ing.amount == ""

    def test_chinese_numeral_is_not_amount(self):
        ing = parse_ingredient_line("五花肉")
        assert ing.name == "五花肉"
        assert ing.amount == ""

    def test_markdown_link_stripped(self):
        ing = parse_ingredient_line("[小炒肉调料](/配料/小炒肉调料.md)")
        assert ing.name == "小炒肉调料"

    def test_empty_returns_none(self):
        assert parse_ingredient_line("   ") is None


class TestParseMarkdown:
    def test_standard_format(self):
        md = "# 粉蒸肉\n\n## 配料\n- 肉片\n- 米粉\n\n## 步骤\n- 1. 拌匀；\n- 2. 蒸 40 分钟。\n"
        out = parse_markdown(md)
        assert out["title"] == "粉蒸肉"
        assert len(out["ingredients"]) == 2
        assert len(out["steps"]) == 2
        assert out["steps"][0].order == 1

    @pytest.mark.parametrize("heading", ["## 原料", "## 原料：", "## 原料:", "## 材料"])
    def test_ingredient_heading_variants(self, heading):
        out = parse_markdown(f"# 菜\n\n{heading}\n- 盐\n")
        assert len(out["ingredients"]) == 1

    @pytest.mark.parametrize("heading", ["## 步骤", "## 步骤：", "## 做法"])
    def test_step_heading_variants(self, heading):
        out = parse_markdown(f"# 菜\n\n{heading}\n- 1. 炒熟\n")
        assert len(out["steps"]) == 1

    def test_image_line_ignored(self):
        out = parse_markdown("# 菜\n\n![菜](../images/x.png)\n\n## 配料\n- 盐\n")
        assert len(out["ingredients"]) == 1

    def test_unknown_section_goes_to_extra(self):
        out = parse_markdown("# 菜\n\n## 营养成分\n- 蛋白质 10g\n")
        assert "营养成分" in out["extra"]


class TestDeriveTags:
    def test_category_drives_method(self):
        tags = derive_tags("粉蒸肉", category="蒸菜")
        assert "蒸" in tags["method"]

    def test_stirring_is_not_cold_dish(self):
        tags = derive_tags("粉蒸肉", category="蒸菜", steps=["将肉片和酱搅拌均匀"])
        assert "凉拌" not in tags["method"]

    def test_fish_flavor_is_not_fish(self):
        tags = derive_tags("鱼香肉丝", category="炒菜", ingredients=["鱼香风味酱"])
        assert "鱼虾" not in tags["ingredient"]

    def test_real_fish_detected(self):
        tags = derive_tags("酸菜鱼", category="煮锅", ingredients=["草鱼片"])
        assert "鱼虾" in tags["ingredient"]

    def test_mala_suppresses_xiangla(self):
        tags = derive_tags("麻婆豆腐", ingredients=["花椒", "干辣椒"])
        assert "麻辣" in tags["flavor"]
        assert "香辣" not in tags["flavor"]

    def test_light_flavor_when_no_heavy_signal(self):
        tags = derive_tags("蒸蛋羹", category="蒸菜", ingredients=["鸡蛋", "盐"])
        assert "清淡" in tags["flavor"]

    def test_heavy_flavor_excludes_light(self):
        tags = derive_tags("小炒肉", ingredients=["螺丝椒", "干辣椒"])
        assert "清淡" not in tags["flavor"]

    def test_beef_slices_not_tagged_pork(self):
        # 回归：「取 55g 红烧牛肉片」曾让「胡萝卜牛肉」同时挂上猪肉标签
        tags = derive_tags("胡萝卜牛肉", category="蒸菜", ingredients=["红烧牛肉"], steps=["取 55g 红烧牛肉片平铺"])
        assert "猪肉" not in tags["ingredient"]
        assert "牛肉" in tags["ingredient"]

    def test_bare_meat_slices_are_pork(self):
        # 不带前缀的「肉片」默认仍是猪肉
        tags = derive_tags("蒜泥白肉", ingredients=["肉片"])
        assert "猪肉" in tags["ingredient"]

    def test_light_never_coexists_with_other_flavor(self):
        # 回归：曾出现「咸鲜 + 清淡」这种自相矛盾的组合
        tags = derive_tags("肉饼蒸蛋", category="蒸菜", ingredients=["生抽", "老鸡汤", "鸡蛋"])
        assert "咸鲜" in tags["flavor"]
        assert "清淡" not in tags["flavor"]

    def test_spicy_title_without_chili_in_ingredients(self):
        # 辣味藏在半成品浇头里时，靠标题兜底
        tags = derive_tags("香辣牛肉面", category="主食", ingredients=["牛肉浇头", "面条"])
        assert "香辣" in tags["flavor"]
        assert "清淡" not in tags["flavor"]

    def test_derived_tags_are_in_vocabulary(self):
        tags = derive_tags("红烧肉", category="炖菜", ingredients=["五花肉", "冰糖"], steps=["小火炖至软烂"])
        assert set(tags["method"]) <= set(METHOD_TAGS)
        assert set(tags["flavor"]) <= set(FLAVOR_TAGS)


class TestMakeSlug:
    def test_source_prefixed(self):
        assert make_slug("西红柿炒蛋", "manual") == "manual:西红柿炒蛋"

    def test_whitespace_collapsed(self):
        assert make_slug("  西红柿 炒蛋 ", "x") == "x:西红柿炒蛋"

    def test_slash_replaced(self):
        assert "/" not in make_slug("凉拌黄瓜/拍黄瓜", "manual")

    def test_same_title_different_source_differs(self):
        assert make_slug("红烧肉", "manual") != make_slug("红烧肉", "cooklikehoc")


class TestBuildRecipe:
    def test_missing_steps_flagged(self):
        r = build_recipe("# 东方树叶\n\n## 配料\n- 茶\n", category="饮品")
        assert r.parse_status == "needs_convert"

    def test_complete_recipe_parsed(self):
        r = build_recipe("# 炒蛋\n\n## 配料\n- 鸡蛋\n\n## 步骤\n- 1. 炒香\n", category="炒菜")
        assert r.parse_status == "parsed"
        assert r.slug == "cooklikehoc:炒蛋"
        assert "炒" in r.method_tags

    def test_raw_text_preserved(self):
        md = "# 菜\n\n## 步骤\n- 1. 做\n"
        assert build_recipe(md).raw_text == md


class TestRenderKindle:
    def _recipe(self, **kw):
        base = {
            "title": "粉蒸肉", "category": "蒸菜",
            "ingredients": [{"name": "肉片", "amount": "80g", "note": ""}],
            "steps": [{"order": 1, "text": "拌匀"}, {"order": 2, "text": "蒸 40 分钟"}],
            "method_tags": ["蒸"], "flavor_tags": ["咸鲜"],
        }
        base.update(kw)
        return base

    def test_contains_title_and_sections(self):
        out = render_recipe(self._recipe())
        assert "粉蒸肉" in out
        assert "【配料】" in out and "【步骤】" in out

    def test_step_number_not_duplicated(self):
        out = render_recipe(self._recipe(steps=[{"order": 1, "text": "1. 先炒香"}]))
        assert "[1] 先炒香" in out
        assert "1. 1." not in out

    def test_leading_amount_is_not_stripped_as_index(self):
        # 回归：曾用 lstrip("0123456789.、) ") 剥序号，把「30g 米粉…」啃成「g 米粉…」
        out = render_recipe(self._recipe(steps=[{"order": 1, "text": "30g 米粉用 20g 纯净水搅拌均匀"}]))
        assert "30g 米粉" in out

    @pytest.mark.parametrize("raw,expect", [
        ("1. 先炒香", "先炒香"),
        ("2、下锅", "下锅"),
        ("3) 收汁", "收汁"),
        ("10. 出锅", "出锅"),
        ("500g 牛腩冷水下锅", "500g 牛腩冷水下锅"),
        ("2 勺生抽淋入", "2 勺生抽淋入"),
    ])
    def test_step_prefix_cases(self, raw, expect):
        out = render_recipe(self._recipe(steps=[{"order": 1, "text": raw}]))
        assert f"[1] {expect}" in out

    def test_file_has_single_recipe(self):
        out = render_recipe_file(self._recipe())
        assert "粉蒸肉" in out and "MySpiders" in out

    def test_note_included(self):
        assert "少放盐" in render_recipe_file(self._recipe(), note="少放盐")


class TestRecipeFilename:
    """文件名 = 菜名、无日期：同一道菜再推一次直接覆盖 Kindle 上的旧版。"""

    def test_plain_title_no_timestamp(self):
        assert recipe_filename("红烧肉") == "红烧肉.txt"

    def test_title_is_sanitized(self):
        # 菜名可能含斜杠（「凉拌黄瓜/拍黄瓜」），不能变成路径
        assert "/" not in recipe_filename("凉拌黄瓜/拍黄瓜")

    def test_same_batch_duplicates_do_not_overwrite_each_other(self):
        # 同一批里两道同名菜（manual:麻婆豆腐 与 howtocook:麻婆豆腐）
        # 若都叫「麻婆豆腐.txt」，后推的会把先推的盖掉，等于静默少推一道
        taken: set = set()
        a = recipe_filename("麻婆豆腐", taken=taken, source="manual")
        b = recipe_filename("麻婆豆腐", taken=taken, source="howtocook")
        assert a == "麻婆豆腐.txt"
        assert b != a and b.endswith(".txt")

    def test_no_double_extension(self):
        assert recipe_filename("x.txt") == "x.txt"


class TestUserOwnedFields:
    """upsert 写的是完整模型，默认值会覆盖掉用户资产。"""

    def test_user_assets_protected(self):
        from app.features.recipe.repo import _USER_OWNED

        # last_pushed_at 不保护的话，一次 AI 重转换就会抹掉「推送过」标记
        for field in ("custom_tags", "favorite", "last_pushed_at"):
            assert field in _USER_OWNED


class TestMineFilter:
    """「我的」= 非批量导入源，用排除法：新录入入口自动算「我的」。"""

    def test_mine_excludes_imported_sources(self):
        from app.features.recipe.repo import IMPORTED_SOURCES, RecipeRepository

        q = RecipeRepository._build_query(mine=True)
        assert q["source"] == {"$nin": list(IMPORTED_SOURCES)}
        assert "howtocook" in IMPORTED_SOURCES

    def test_explicit_source_wins(self):
        from app.features.recipe.repo import RecipeRepository

        assert RecipeRepository._build_query(mine=True, source="manual")["source"] == "manual"


class TestSafeFilename:
    """shlex.quote 挡得住 shell 注入，挡不住路径穿越 —— 引用后的 ../ 仍是合法路径。"""

    def test_strips_directory_traversal(self):
        assert _safe_filename("../../mnt/us/koreader/settings/x.txt") == "x.txt"

    def test_strips_absolute_path(self):
        assert _safe_filename("/etc/passwd") == "passwd"

    def test_strips_backslash_path(self):
        assert _safe_filename("..\\..\\evil.txt") == "evil.txt"

    def test_empty_falls_back(self):
        assert _safe_filename("") == "今日菜单.txt"
        assert _safe_filename("..") == "今日菜单.txt"

    def test_normal_name_kept(self):
        assert _safe_filename("周三晚餐.txt") == "周三晚餐.txt"


class TestDuplicateSteps:
    def test_identical_step_texts_get_distinct_numbers(self):
        # 回归：曾用 steps.index(step) 定位，按值相等查找会让重复内容都拿到第一个位置
        out = render_recipe({
            "title": "菜",
            "steps": [{"text": "翻炒均匀"}, {"text": "翻炒均匀"}, {"text": "出锅"}],
        })
        assert "[1] 翻炒均匀" in out
        assert "[2] 翻炒均匀" in out
        assert "[3] 出锅" in out


class TestConvertSanitize:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_prose_around(self):
        assert _extract_json('好的：\n{"a": 1}\n希望有帮助') == {"a": 1}

    def test_garbage_returns_none(self):
        assert _extract_json("完全不是 JSON") is None

    def test_sanitize_drops_invented_tags(self):
        assert _sanitize_tags(["炒", "家常菜", "蒸"], METHOD_TAGS) == ["炒", "蒸"]

    def test_sanitize_dedups_preserving_order(self):
        assert _sanitize_tags(["炒", "炒", "蒸"], METHOD_TAGS) == ["炒", "蒸"]

    def test_sanitize_handles_none(self):
        assert _sanitize_tags(None, METHOD_TAGS) == []


class TestBuildRecipeFromPayload:
    def test_string_steps_accepted(self):
        recipe, _, _ = build_recipe_from_payload({"title": "菜", "steps": ["切", "炒"]})
        assert [s.order for s in recipe.steps] == [1, 2]

    def test_string_ingredients_accepted(self):
        recipe, _, _ = build_recipe_from_payload({"title": "菜", "ingredients": ["盐", "油"]})
        assert [i.name for i in recipe.ingredients] == ["盐", "油"]

    def test_tags_fall_back_to_derivation(self):
        recipe, warnings, _ = build_recipe_from_payload(
            {"title": "麻婆豆腐", "category": "炒菜", "ingredients": [{"name": "花椒"}], "steps": ["翻炒"]}
        )
        assert recipe.flavor_tags
        assert any("回退" in w for w in warnings)

    def test_bad_minutes_becomes_none(self):
        recipe, _, _ = build_recipe_from_payload({"title": "菜", "total_minutes": "大概三十分钟"})
        assert recipe.total_minutes is None

    def test_missing_title_gets_placeholder(self):
        recipe, _, _ = build_recipe_from_payload({})
        assert recipe.title == "未命名菜谱"

    def test_status_marked_converted(self):
        recipe, _, _ = build_recipe_from_payload({"title": "菜", "steps": ["炒"]})
        assert recipe.parse_status == "converted"

    def test_custom_vocab_accepted(self):
        # 用户新加的标签（词表存库）必须能通过白名单，不能被硬编码常量拒掉
        recipe, _, _ = build_recipe_from_payload(
            {"title": "菜", "steps": ["炸"], "method_tags": ["空气炸锅"]},
            vocab={"method": ["空气炸锅"], "flavor": ["清淡"], "ingredient": ["蔬菜"]},
        )
        assert "空气炸锅" in recipe.method_tags


class TestAnswers:
    """用户用 [] 夹带提问（「盐 [对500g鸡腿用多少]」），
    模型的回答必须进到 tips 里，否则用户根本看不到。"""

    def test_answers_prepended_to_tips(self):
        recipe, _, answers = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "tips": "全程大火",
            "answers": [{"question": "味极鲜替代", "answer": "2份生抽+少许糖", "where": "配料:味极鲜"}],
        })
        assert answers[0]["answer"] == "2份生抽+少许糖"
        assert "味极鲜替代" in recipe.tips
        assert "全程大火" in recipe.tips
        # 回答排在前面：用户是带着问题来的
        assert recipe.tips.index("味极鲜替代") < recipe.tips.index("全程大火")

    def test_where_is_shown(self):
        # 问题出现在哪个字段要带出来，否则多个提问时对不上号
        recipe, _, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "answers": [{"question": "放多少", "answer": "5g", "where": "配料:盐"}],
        })
        assert "配料:盐" in recipe.tips

    def test_answers_without_tips(self):
        recipe, _, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "answers": [{"question": "要焯水吗", "answer": "要"}],
        })
        assert "要焯水吗" in recipe.tips

    def test_no_answers_leaves_tips_alone(self):
        recipe, _, answers = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"], "tips": "全程大火",
        })
        assert answers == []
        assert recipe.tips == "全程大火"

    def test_blank_answers_filtered(self):
        _, _, answers = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"], "answers": ["", "  ", "有效回答"],
        })
        assert [a["answer"] for a in answers] == ["有效回答"]

    def test_legacy_string_answers_still_work(self):
        # 提示词改成要对象了，但模型偶尔仍退化成字符串数组；不能因此丢掉回答
        recipe, _, answers = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"], "answers": ["旧格式回答"],
        })
        assert answers == [{"question": "", "answer": "旧格式回答", "where": ""}]
        assert "旧格式回答" in recipe.tips

    def test_missing_answer_is_warned(self):
        # 模型漏答是静默的 —— 用户以为 AI 看到了，其实没有。必须吵一声。
        _, warnings, _ = build_recipe_from_payload(
            {"title": "菜", "steps": ["炒"], "ingredients": [{"name": "盐", "amount": "5g"}]},
            raw_text="盐 [对500g鸡腿用多少] 另外[能提前做好吗]",
        )
        assert any("提问" in w for w in warnings)


class TestExtractQuestions:
    """抽提问用于核对模型有没有漏答，口径要和前端 countPendingQuestions 一致。"""

    def test_halfwidth_brackets(self):
        assert extract_questions("盐 [对500g鸡腿用多少]") == ["对500g鸡腿用多少"]

    def test_fullwidth_brackets(self):
        assert extract_questions("盐 ［用多少］") == ["用多少"]

    def test_multiple_questions(self):
        qs = extract_questions("盐[放多少]\n提前做好[能冷藏吗]")
        assert qs == ["放多少", "能冷藏吗"]

    def test_legacy_paren_question_still_counted(self):
        # 早期用法是圆括号带问号，改推方括号不能让老条目的提问漏掉
        assert extract_questions("味极鲜（家里没有，怎么代替？）") == ["家里没有，怎么代替？"]

    def test_plain_paren_note_is_not_question(self):
        # 正常括注（产地、品牌）不带问号，不能误判成提问
        assert extract_questions("豆瓣酱（郫县）") == []

    def test_dedup(self):
        assert extract_questions("[放多少] 和 [放多少]") == ["放多少"]


class TestQuestionStripping:
    """输出里不许残留方括号提问 —— 否则配料表会出现一句问句当用量，
    推到 Kindle 上尤其难看。"""

    def test_bracket_stripped_from_amount(self):
        recipe, _, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "ingredients": [{"name": "盐", "amount": "[对500g鸡腿用多少]", "note": ""}],
        })
        assert "[" not in recipe.ingredients[0].amount

    def test_bracket_stripped_from_step(self):
        recipe, _, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["大火翻炒[多大火？]"],
        })
        assert "[" not in recipe.steps[0].text
        assert "大火翻炒" in recipe.steps[0].text

    def test_normal_paren_note_preserved(self):
        # 只剥方括号，圆括号括注是有用信息（产地、品牌）
        recipe, _, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "ingredients": [{"name": "豆瓣酱", "amount": "20g", "note": "郫县（红油）"}],
        })
        assert recipe.ingredients[0].note == "郫县（红油）"


class TestBlankAmountWarning:
    """用量留空是模型最常见的退化：一忙着答疑就忘了推算。
    家常菜谱里一味没有量的料等于没写，不能静默放过。"""

    def test_blank_amount_warns(self):
        _, warnings, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "ingredients": [{"name": "盐", "amount": ""}, {"name": "鸡腿", "amount": "500g"}],
        })
        assert any("用量" in w for w in warnings)

    def test_all_amounts_given_no_warning(self):
        _, warnings, _ = build_recipe_from_payload({
            "title": "菜", "steps": ["炒"],
            "ingredients": [{"name": "盐", "amount": "5g"}],
        })
        assert not any("用量" in w for w in warnings)


class TestRenderDocForConvert:
    """AI 重转必须喂**当前库里的内容**，不是导入时的 raw_text ——
    后者不含用户的任何编辑，用它重转就是「把我存好的用量覆盖掉」的根因。"""

    def _doc(self, **kw):
        base = {
            "title": "可乐鸡翅",
            "category": "荤菜",
            "ingredients": [
                {"name": "鸡腿", "amount": "500g", "note": ""},
                {"name": "盐", "amount": "[对500g鸡腿用多少]", "note": ""},
                {"name": "姜", "amount": "", "note": ""},
            ],
            "steps": [{"order": 1, "text": "焯水"}],
            "tips": "[能提前煮好冷藏吗]",
        }
        base.update(kw)
        return base

    def test_user_edited_amount_is_fed_to_model(self):
        # 用户改过的 500g 必须出现在输入里，模型才可能保留它
        assert "500g" in render_doc_for_convert(self._doc())

    def test_questions_are_fed_to_model(self):
        out = render_doc_for_convert(self._doc())
        assert "[对500g鸡腿用多少]" in out
        assert "[能提前煮好冷藏吗]" in out

    def test_blank_amount_marked_explicitly(self):
        # 空用量写成「未填」而不是留白：模型对"字段存在但空着"反应积极得多
        assert "未填" in render_doc_for_convert(self._doc())

    def test_field_structure_is_labelled(self):
        # 要让模型知道哪个 [] 挂在哪个字段上，答案才能落回原处
        out = render_doc_for_convert(self._doc())
        assert "用量:" in out
        assert "【小贴士】" in out

    def test_empty_doc_yields_no_content(self):
        out = render_doc_for_convert({"title": "", "ingredients": [], "steps": []})
        assert out.strip() in ("", "菜名：")
