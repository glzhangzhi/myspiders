"""配料汇总（采买清单）的纯函数测试。

这些用例全部来自**库里真实存在的脏数据**（371 道 HowToCook 菜谱），
不是臆想的边界 —— 用量写法有二十多种变体，解析规则一改就容易顾此失彼。
"""

import pytest

from app.features.recipe.shopping import (
    aggregate,
    classify,
    format_quantity,
    normalize_name,
    parse_amount,
    split_inverted,
    strip_leading_qty,
)


class TestParseAmount:
    @pytest.mark.parametrize("raw,unit,lo,hi", [
        ("200g", "g", 200, 200),
        ("10-15ml", "ml", 10, 15),
        ("约 3 斤", "g", 1500, 1500),          # 斤 → g
        ("1.5kg", "g", 1500, 1500),
        ("2 克", "g", 2, 2),
        ("10 ～ 12 只", "只", 10, 12),          # 全角波浪线也是区间
        ("5-8 瓣", "瓣", 5, 8),
        ("两片", "片", 2, 2),                  # 中文数字
        ("半根", "根", 0.5, 0.5),
        ("1500ml", "ml", 1500, 1500),
    ])
    def test_quantities(self, raw, unit, lo, hi):
        qty, _ = parse_amount(raw)
        assert qty == {unit: [lo, hi]}

    @pytest.mark.parametrize("raw", ["1 个（约 180g） * 份数", "份数 * 3g。", "0-2g * 份数"])
    def test_serving_factor_is_stripped(self, raw):
        # 「* 份数」是 HowToCook 的公式语法，当前菜单没有份数概念，按 1 份算。
        # 剥不掉的话数字后面拖着公式，根本匹配不上单位。
        qty, _ = parse_amount(raw)
        assert qty and "份数" not in str(qty)

    def test_only_first_quantity_is_taken(self):
        # 「5 克、生抽 10 克、蒜蓉 5 克」是把整串调料挤进一个用量字段的脏数据；
        # 后面那些量的主语不是本条配料，加进来就是张冠李戴。
        qty, _ = parse_amount("5 克、生抽 10 克、蒜蓉 5 克")
        assert qty == {"g": [5.0, 5.0]}

    @pytest.mark.parametrize("raw", ["适量", "少许", "随意"])
    def test_wordy_amount_is_uncertain(self, raw):
        qty, uncertain = parse_amount(raw)
        assert qty == {} and uncertain is True

    @pytest.mark.parametrize("raw", ["4ml * 鸡蛋/个", "20g/人"])
    def test_variable_formula_flagged_uncertain(self, raw):
        # 用量依赖别的变量，求和结果只是下限 —— 必须让界面能标出来，
        # 而不是假装它精确。
        qty, uncertain = parse_amount(raw)
        assert qty and uncertain is True

    def test_empty(self):
        assert parse_amount("") == ({}, False)

    def test_longest_unit_wins(self):
        # 「毫升」不能被「升」先吃掉一半 —— 那会把 50 毫升读成 50 升
        assert parse_amount("50 毫升") == ({"ml": [50.0, 50.0]}, False)


class TestFormatQuantity:
    @pytest.mark.parametrize("unit,lo,hi,expect", [
        ("g", 200, 200, "200g"),
        ("g", 1200, 1200, "1.2kg"),        # 上千自动进位，采买看着才有概念
        ("ml", 10, 15, "10-15ml"),
        ("ml", 1500, 1500, "1.5L"),
        ("个", 2, 2, "2 个"),               # 中文计数单位要空格
        ("瓣", 5, 8, "5-8 瓣"),
        ("g", 0.5, 0.5, "0.5g"),
    ])
    def test_format(self, unit, lo, hi, expect):
        assert format_quantity(unit, lo, hi) == expect


class TestNormalizeName:
    def test_nested_parens(self):
        # 回归：非贪婪正则会在第一个右括号收尾，把「、烟台欣和）」留在名字里，
        # 那样它永远合并不上别处的「小炒肉调料」。
        name, note = normalize_name("小炒肉调料（聚慧（重庆）、烟台欣和）")
        assert name == "小炒肉调料"
        assert "烟台欣和" in note

    def test_optional_marker_to_note(self):
        assert normalize_name("糖（可选）") == ("糖", "可选")

    def test_markdown_image_removed(self):
        name, _ = normalize_name("泡发好的海参![海参](./海参.jpeg)")
        assert name == "泡发好的海参"

    def test_cross_reference_stripped(self):
        # 源仓库爱挂交叉引用，它会让同一样东西因后缀不同而合并不上
        assert normalize_name("牛排，参见[如何选择不同种类的牛排]")[0] == "牛排"

    def test_trailing_amount_stripped(self):
        # CookLikeHOC 那几道把用量写进名字，剥掉才能与别处的「里脊肉」合并
        assert normalize_name("里脊肉 200g")[0] == "里脊肉"


class TestSplitInverted:
    """HowToCook 的「1 枚咸鸭蛋」被上游按空格切成 name="1" / amount="枚咸鸭蛋"。"""

    @pytest.mark.parametrize("name,amount,expect", [
        ("1", "枚咸鸭蛋", ("咸鸭蛋", "1 枚")),
        ("20-30g", "五花肉", ("五花肉", "20-30g")),
        ("2", "片生姜", ("生姜", "2 片")),
        ("5g", "蒜蓉辣酱", ("蒜蓉辣酱", "5g")),
    ])
    def test_inverted_rows_are_fixed(self, name, amount, expect):
        assert split_inverted(name, amount) == expect

    @pytest.mark.parametrize("name,amount", [("盐", "3g"), ("里脊肉", "200g"), ("五花肉", "")])
    def test_normal_rows_untouched(self, name, amount):
        assert split_inverted(name, amount) == (name, amount)


class TestStripLeadingQty:
    def test_qty_prefix_moves_to_amount(self):
        assert strip_leading_qty("10g 吉利丁", "吉利丁") == ("吉利丁", "10g")

    def test_does_not_discard_existing_amount(self):
        # 名字前缀与已有 amount 冲突时宁可不动 —— 不能悄悄改掉用户看得见的数字
        assert strip_leading_qty("2 个西红柿", "500g") == ("2 个西红柿", "500g")

    def test_plain_name_untouched(self):
        assert strip_leading_qty("五花肉", "") == ("五花肉", "")


class TestClassify:
    @pytest.mark.parametrize("name,group", [
        ("五花肉", "protein"), ("鸡蛋", "protein"), ("鸡翅中", "protein"),
        ("西红柿", "fresh"), ("土豆", "fresh"), ("香菇", "fresh"),
        ("面粉", "staple"), ("豆腐", "staple"),
        ("盐", "pantry"), ("蚝油", "pantry"), ("干辣椒", "pantry"),
        ("炒锅", "tool"), ("菜刀", "tool"), ("烤箱", "tool"),
    ])
    def test_groups(self, name, group):
        assert classify(name) == group

    def test_oil_suffix_does_not_swallow_rape_greens(self):
        # `油$` 要命中「食用油 / 蚝油」而放过「油菜」—— 否则青菜被排进调味区
        assert classify("油菜") == "fresh"
        assert classify("食用油") == "pantry"

    def test_hotpot_base_is_seasoning_not_cookware(self):
        # 「火锅底料」含「锅」，但它是调料不是厨具
        assert classify("火锅底料") == "pantry"

    @pytest.mark.parametrize("name", ["汤匙", "茶匙", "量杯", "蒸锅"])
    def test_measuring_tools_are_cookware(self, name):
        # 量具曾掉进「其他」，混在采买清单里让人以为要买
        assert classify(name) == "tool"

    @pytest.mark.parametrize("name", ["牛排", "猪排"])
    def test_cuts_recognised_as_protein(self, name):
        # 回归：recipe_tags 的主材词表漏了这两个，牛排整道菜一个主材标签都没有，
        # 采买清单也认不出它是肉（曾被排进「其他」）
        assert classify(name) == "protein"


class TestAggregate:
    def _dish(self, title, ings):
        return {"title": title, "ingredients": [
            {"name": n, "amount": a, "note": ""} for n, a in ings
        ]}

    def test_same_ingredient_merged_and_summed(self):
        res = aggregate([
            self._dish("A", [("盐", "3g")]),
            self._dish("B", [("盐", "2g")]),
        ])
        salt = next(i for g in res["groups"] for i in g["items"] if i["name"] == "盐")
        assert salt["amount"] == "5g"
        assert salt["dishes"] == ["A", "B"]

    def test_ranges_sum_on_both_ends(self):
        res = aggregate([
            self._dish("A", [("食用油", "10-15ml")]),
            self._dish("B", [("食用油", "5ml")]),
        ])
        oil = next(i for g in res["groups"] for i in g["items"] if i["name"] == "食用油")
        assert oil["amount"] == "15-20ml"

    def test_incompatible_units_listed_side_by_side(self):
        # 「2 个西红柿」和「180g 西红柿」不相加：一个计数一个重量，
        # 硬折算要假设"一个西红柿多重"，猜错了比不合更糟。
        res = aggregate([
            self._dish("A", [("西红柿", "2 个")]),
            self._dish("B", [("西红柿", "180g")]),
        ])
        item = next(i for g in res["groups"] for i in g["items"] if i["name"] == "西红柿")
        assert "2 个" in item["amount"] and "180g" in item["amount"]

    def test_aliases_merge(self):
        res = aggregate([
            self._dish("A", [("食盐", "3g")]),
            self._dish("B", [("盐", "2g")]),
        ])
        names = [i["name"] for g in res["groups"] for i in g["items"]]
        assert names.count("盐") == 1 and "食盐" not in names

    def test_similar_but_distinct_not_merged(self):
        # 白糖与冰糖在厨房里是两样东西，模糊合并会让人少买一样
        res = aggregate([self._dish("A", [("白糖", "10g"), ("冰糖", "10g")])])
        names = [i["name"] for g in res["groups"] for i in g["items"]]
        assert "白糖" in names and "冰糖" in names

    def test_optional_only_when_all_sources_optional(self):
        res = aggregate([
            {"title": "A", "ingredients": [{"name": "葱花", "amount": "", "note": "可选"}]},
            {"title": "B", "ingredients": [{"name": "葱花", "amount": "", "note": ""}]},
        ])
        item = next(i for g in res["groups"] for i in g["items"] if i["name"] == "葱花")
        assert item["optional"] is False

    def test_formula_residue_not_shown_as_amount(self):
        # 「（份数*1.5 // 1 向下取整）」是没算完的公式，不是用量说明
        res = aggregate([self._dish("A", [("鸡蛋", "（份数*1.5 // 1 向下取整）")])])
        item = next(i for g in res["groups"] for i in g["items"] if i["name"] == "鸡蛋")
        assert "//" not in item["amount"] and "取整" not in item["amount"]

    def test_bare_unit_not_shown_as_amount(self):
        res = aggregate([self._dish("A", [("火腿", "个")])])
        item = next(i for g in res["groups"] for i in g["items"] if i["name"] == "火腿")
        assert item["amount"] == ""

    def test_prose_row_dropped(self):
        # 源仓库把注意事项写进了原料表，它不是配料
        res = aggregate([self._dish("A", [
            ("注：如果有可能，请尽量把刀磨的锋利一些。", ""), ("盐", "3g"),
        ])])
        names = [i["name"] for g in res["groups"] for i in g["items"]]
        assert names == ["盐"]

    def test_shared_ingredients_sort_first(self):
        res = aggregate([
            self._dish("A", [("盐", "1g"), ("八角", "1 个")]),
            self._dish("B", [("盐", "1g")]),
        ])
        pantry = next(g for g in res["groups"] if g["key"] == "pantry")
        assert pantry["items"][0]["name"] == "盐"

    def test_tools_grouped_last(self):
        res = aggregate([self._dish("A", [("炒锅", ""), ("盐", "3g")])])
        assert res["groups"][-1]["key"] == "tool"

    def test_empty_menu(self):
        assert aggregate([]) == {"groups": [], "total": 0, "dishes": 0}

    def test_dish_without_ingredients(self):
        assert aggregate([{"title": "空菜", "ingredients": []}])["total"] == 0
