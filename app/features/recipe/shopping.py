"""当前菜单 → 配料汇总（采买清单）。

为什么在服务层算而不是前端：源数据里用量的写法有二十多种变体
（`200g` / `10-15ml` / `1 个（约 180g） * 份数` / `份数 * 3g。` / `两片` /
`适量`），解析规则必须能被测试盯住；前端只负责画。

做三件事：

1. **同名合并** —— 一顿饭三道菜都放盐，清单上只该出现一行
2. **同单位求和** —— 能加的加起来（g / ml / 个…），加不动的原样并列
3. **按采买区分组** —— 蔬果 / 肉蛋水产 / 米面豆制品 / 调味常备 / 其他 / 厨具。
   照超市动线排；调味料和厨具家里多半已有，排在后面

**不做单位换算的跨类合并**：「2 个西红柿」和「180g 西红柿」不相加 ——
一个个数一个重量，硬折算要引入"一个西红柿多重"的假设，猜错了比不合更糟。
两种都留着并列显示，人站在菜摊前一眼能判断。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from app.features.recipe.hoc_import import _split_parens
from app.features.recipe.tags import derive_tags

# ── 数量解析 ──

# 中文数字：源数据里「两片姜」「半根胡萝卜」「一把香菜」不算少见。
# 只收十以内 —— 再大的写法源数据里就是阿拉伯数字了。
_CN_NUM = {
    "半": 0.5, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 单位 → (规范单位, 换算到规范单位的系数)。
#
# 只有**质量**与**体积**两族做换算，个 / 根 / 片这类计数单位各归各的：
# 「3 瓣蒜」和「1 头蒜」不能相加，强行折算需要"一头蒜几瓣"的假设。
_UNITS: dict[str, tuple[str, float]] = {
    "g": ("g", 1), "G": ("g", 1), "克": ("g", 1), "公克": ("g", 1),
    "kg": ("g", 1000), "KG": ("g", 1000), "Kg": ("g", 1000),
    "千克": ("g", 1000), "公斤": ("g", 1000),
    "斤": ("g", 500), "两": ("g", 50),
    "ml": ("ml", 1), "mL": ("ml", 1), "ML": ("ml", 1), "Ml": ("ml", 1),
    "cc": ("ml", 1), "毫升": ("ml", 1),
    "l": ("ml", 1000), "L": ("ml", 1000), "升": ("ml", 1000), "公升": ("ml", 1000),
}
# 计数单位：各自独立，不与任何单位互换
_COUNT_UNITS = [
    "汤匙", "茶匙", "汤勺", "茶勺", "大勺", "小勺", "小块", "小把", "人份",
    "个", "根", "片", "瓣", "颗", "只", "块", "把", "棵", "条", "段", "朵",
    "粒", "包", "罐", "盒", "袋", "碗", "杯", "张", "滴", "撮", "支", "枚",
    "头", "扎", "勺", "份", "串", "listed",
]
for _u in _COUNT_UNITS:
    _UNITS.setdefault(_u, (_u, 1))

_NUM = r"(?:\d+(?:\.\d+)?|[半一两二三四五六七八九十])"
# 长单位优先，否则「毫升」会被「升」先吃掉一半
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
_QTY_RE = re.compile(
    rf"(?P<lo>{_NUM})\s*(?:[-~～－—至到]\s*(?P<hi>{_NUM})\s*)?(?P<unit>{_UNIT_ALT})"
)

# 「* 份数」是 HowToCook 的用量公式语法（见 recipe_howtocook）。当前菜单没有
# 份数概念，按 1 份算 —— 剥掉这个因子才能露出真正的数量。
_SERVING_FACTOR = re.compile(r"(?:[*×]\s*份数|份数\s*[*×])")
# 剥完还剩乘号 / 「每人」，说明用量依赖别的变量（「4ml * 鸡蛋/个」），
# 算出来的数只是下限，要在界面上标出来而不是假装精确。
_STILL_VARIABLE = re.compile(r"[*×]|/\s*人|每人")

_TRAILING_QTY = re.compile(rf"\s+({_NUM}\s*(?:{_UNIT_ALT}))\s*$")
# 整个字段就是一个数量（没有主语）——上游解析器倒错行的特征，见 split_inverted
_PURE_QTY = re.compile(rf"^\s*{_NUM}\s*(?:[-~～－—至到]\s*{_NUM}\s*)?(?:{_UNIT_ALT})?\s*$")
_LEADING_QTY = re.compile(
    rf"^\s*(?P<qty>{_NUM}\s*(?:[-~～－—至到]\s*{_NUM}\s*)?(?:{_UNIT_ALT}))\s*(?P<rest>.+)$"
)


def _num(text: str) -> Optional[float]:
    if text in _CN_NUM:
        return _CN_NUM[text]
    try:
        return float(text)
    except ValueError:
        return None


def parse_amount(text: str) -> tuple[dict[str, list[float]], bool]:
    """用量原文 → ({规范单位: [下限, 上限]}, 是否不确定)。

    只取**第一个**数量：「5 克、生抽 10 克」这种把整串调料挤进一个用量字段的
    脏数据，后面那些量的主语并不是本条配料，加进来等于张冠李戴。
    """
    raw = (text or "").strip()
    if not raw:
        return {}, False

    stripped = _SERVING_FACTOR.sub("", raw)
    m = _QTY_RE.search(stripped)
    if not m:
        # 「适量」「少许」这类没有数字的说明，交给调用方原样展示
        return {}, True

    lo = _num(m.group("lo"))
    if lo is None:
        return {}, True
    hi = _num(m.group("hi") or "") if m.group("hi") else lo
    if hi is None or hi < lo:
        hi = lo

    canon, factor = _UNITS[m.group("unit")]
    return {canon: [lo * factor, hi * factor]}, bool(_STILL_VARIABLE.search(stripped))


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_quantity(unit: str, lo: float, hi: float) -> str:
    """(g, 1200, 1200) → `1.2kg`；(ml, 10, 15) → `10-15ml`。"""
    label = unit
    if unit == "g" and lo >= 1000:
        lo, hi, label = lo / 1000, hi / 1000, "kg"
    elif unit == "ml" and lo >= 1000:
        lo, hi, label = lo / 1000, hi / 1000, "L"

    body = _fmt_num(lo) if abs(hi - lo) < 1e-6 else f"{_fmt_num(lo)}-{_fmt_num(hi)}"
    # 计数单位在中文里要跟名字隔开（「2 个」），g/ml 这类拉丁单位紧贴（「200g」）
    return f"{body}{label}" if label in ("g", "kg", "ml", "L") else f"{body} {label}"


# ── 名字规范化 ──

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 整行其实是句说明而不是配料（源仓库把注意事项写进了原料表）
_PROSE = re.compile(r"^(注[：:]|PS|ps|※|如果|建议|可根据)")
# 源仓库爱在原料名后挂交叉引用（「牛排，参见[如何选择不同种类的牛排]」）。
# 引用对采买没用，且会让同一样东西因为后缀不同而合并不上。
_CROSS_REF = re.compile(r"[，,]\s*(?:参见|详见|见|另见).*$")

# 同物异名。**只做完全相等的替换**，不做包含匹配 ——
# 「白糖」与「冰糖」在厨房里是两样东西，模糊合并会让人少买一样。
_ALIASES = {
    "食盐": "盐", "食用盐": "盐", "精盐": "盐",
    "白砂糖": "白糖", "绵白糖": "白糖", "砂糖": "白糖",
    "植物油": "食用油", "油": "食用油",
    "大蒜": "蒜", "蒜瓣": "蒜", "蒜头": "蒜",
    "生姜": "姜", "老姜": "姜", "姜块": "姜",
    "番茄": "西红柿", "圣女果": "小番茄",
    "清水": "水", "纯净水": "水", "凉水": "水", "温水": "水", "开水": "水",
    "酱油": "生抽",
    "马铃薯": "土豆", "洋芋": "土豆",
    "小米辣": "小米椒",
}


def normalize_name(raw: str) -> tuple[str, str]:
    """配料名 → (规范名, 括注)。名字里夹带的用量也在这里剥出来。"""
    name = _MD_IMAGE.sub("", raw or "")
    name = _MD_LINK.sub(r"\1", name)

    # 复用 recipe_import 的深度扫描切分：源数据里括注套括注很常见
    # （「小炒肉调料（聚慧（重庆）、烟台欣和）」），非贪婪正则会在第一个
    # 右括号处收尾，把「、烟台欣和）」留在主料名里 —— 那样它就永远合并不上。
    notes, name = _split_parens(name)
    name = name.replace("\u3000", " ").strip().strip("、，,。.：: *")

    name = _CROSS_REF.sub("", name).strip()
    # CookLikeHOC 那几道把用量写在名字里（「里脊肉 200g」），剥掉才能和
    # 别处的「里脊肉」合并到一行
    name = _TRAILING_QTY.sub("", name).strip()
    return name, "；".join(n.strip() for n in notes if n.strip())


def split_inverted(name: str, amount: str) -> tuple[str, str]:
    """修正上游倒错的 name/amount。

    HowToCook 的「计算」段里有「1 枚咸鸭蛋」这种**量在前、名在后**的写法，
    而解析器按第一个空格切分，得到 `name="1", amount="枚咸鸭蛋"` —— 数量成了
    名字。这类行在库里有几十条，不修的话采买清单上会出现「1」「20-30g」
    这种没有主语的条目，而真正的「咸鸭蛋」又少算了一份量。

    只在**名字整体就是个数量**时才动手，正常行原样返回。
    """
    name, amount = (name or "").strip(), (amount or "").strip()
    if not name or not amount or not _PURE_QTY.match(name):
        return name, amount
    m = _LEADING_QTY.match(f"{name} {amount}")
    if not m or not m.group("rest").strip():
        return name, amount
    return m.group("rest").strip(), m.group("qty").strip()


def strip_leading_qty(name: str, amount: str) -> tuple[str, str]:
    """名字带着用量前缀时把它挪到 amount（「10g 吉利丁」→ 吉利丁 / 10g）。

    与 split_inverted 分开：那个处理"名字整个是数量"，这个处理"名字前面粘了
    数量"。都只在**不丢信息**时动手 —— amount 已有内容且与前缀不同就不碰，
    宁可显示得笨一点也不要悄悄改掉用户能看到的数字。
    """
    m = _LEADING_QTY.match(name or "")
    if not m:
        return name, amount
    rest = m.group("rest").strip()
    qty = m.group("qty").strip()
    if not rest:
        return name, amount
    if amount and amount.strip() not in (qty, rest):
        return name, amount
    return rest, qty


# 残留用量原文只在**像人话**时才展示：「适量」「少许」有用，
# 「（份数*1.5 // 1 向下取整）」是没算完的公式，摆到采买清单上纯属噪音。
_FORMULA_CHARS = re.compile(r"[*×/=]|//")
_BARE_UNIT = re.compile(rf"^\s*(?:{_UNIT_ALT})\s*$")


def _keep_text_amount(text: str) -> bool:
    if not text or len(text) > 6 or _FORMULA_CHARS.search(text):
        return False
    # 光秃秃一个单位（`个` / `颗`）是公式被剥掉后的残渣，不是用量说明
    return not _BARE_UNIT.match(text)


def _is_noise(name: str) -> bool:
    if not name or len(name) > 24:
        return True
    return bool(_PROSE.match(name))


# ── 采买分区 ──

# 厨具 / 器具：源仓库的「必备原料和工具」把锅铲刀板和食材混在一段里。
# 它们不用买，但也不能删 —— 第一次做某道菜时"家里没有烤箱"是要提前知道的。
_TOOL_PATTERNS = [
    r"锅$", r"锅(?![巴贴])", r"刀", r"案板", r"砧板", r"铲", r"笊篱", r"漏勺", r"滤网",
    r"烤箱", r"微波炉", r"电饭煲", r"蒸屉", r"蒸架", r"烤盘", r"烤网", r"烧烤炉",
    r"打蛋器", r"搅拌器", r"榨汁机", r"料理机", r"模具", r"容器", r"杯子", r"碗$",
    r"盆$", r"盘$", r"筷", r"夹子", r"厨房秤", r"温度计", r"保鲜膜", r"锡箔", r"油纸",
    r"厨房纸", r"烘焙纸", r"牙签", r"手套", r"擀面杖", r"刷子", r"纱布", r"过滤袋",
    r"^汤匙$", r"^茶匙$", r"^量杯$", r"^量勺$", r"蒸锅", r"蒸屉",
    r"高压锅", r"平底锅", r"炒锅", r"深锅", r"煮锅", r"汤锅", r"煲",
]

# 调味 · 常备：家里多半已有，排在生鲜后面。
# 用锚定写法避免误伤 —— `油$` 命中「食用油 / 蚝油 / 橄榄油」而放过「油菜」。
_PANTRY_PATTERNS = [
    r"盐", r"糖(?!心)", r"油$", r"酱$", r"酱油", r"醋", r"酒$", r"生抽", r"老抽",
    r"蚝油", r"鸡精", r"味精", r"胡椒", r"花椒", r"八角", r"桂皮", r"香叶", r"茴香",
    r"五香", r"十三香", r"孜然", r"豆蔻", r"陈皮", r"草果", r"香料", r"底料", r"调料",
    r"干辣椒", r"辣椒粉", r"辣椒面", r"辣椒油", r"剁椒", r"小米辣", r"小米椒",
    r"淀粉", r"生粉", r"蜂蜜", r"豆豉", r"豆瓣", r"芝麻$", r"高汤", r"咖喱", r"芥末",
    r"味极鲜", r"蒸鱼豉油", r"番茄酱", r"料酒", r"^水$", r"食用碱", r"小苏打", r"酵母",
]

# 生鲜里 recipe_tags 的主材词表覆盖不到的常客（葱姜蒜是出现频次最高的几个）
_FRESH_PATTERNS = [
    r"葱", r"姜", r"蒜", r"洋葱", r"香菜", r"芹菜", r"胡萝卜", r"椒", r"瓜", r"笋",
    r"菇", r"耳", r"菜", r"豆(?!瓣|豉)", r"玉米", r"茄", r"藕", r"苗", r"芽", r"薯",
    r"柠檬", r"苹果", r"香蕉", r"菠萝", r"草莓", r"蓝莓", r"橙",
]

_PROTEIN_TAGS = {"猪肉", "牛肉", "羊肉", "鸡肉", "鸭肉", "鱼虾", "海鲜", "内脏", "蛋"}
_STAPLE_TAGS = {"米面", "豆制品"}
_FRESH_TAGS = {"蔬菜", "菌菇", "土豆"}

GROUPS = [
    ("fresh", "🥬 蔬果"),
    ("protein", "🍖 肉蛋水产"),
    ("staple", "🍚 米面豆制品"),
    ("pantry", "🧂 调味 · 常备"),
    ("other", "📦 其他"),
    ("tool", "🍳 厨具（不用买）"),
]


def _hit(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(name: str) -> str:
    """配料名 → 采买分区 key。

    判定顺序是有讲究的：厨具 → 调味 → 生鲜。
    「火锅底料」含「锅」但先被「底料」拦下，「油菜」含「菜」而 `油$` 不命中它。
    """
    if _hit(_TOOL_PATTERNS, name) and not _hit([r"底料", r"调料", r"锅巴"], name):
        return "tool"
    if _hit(_PANTRY_PATTERNS, name):
        return "pantry"

    # 主材判定复用 recipe_tags 的公开接口 —— 什么算「猪肉」只有一处定义
    tags = set(derive_tags(title=name, ingredients=[name])["ingredient"])
    if tags & _PROTEIN_TAGS:
        return "protein"
    if tags & _FRESH_TAGS:
        return "fresh"
    if tags & _STAPLE_TAGS:
        return "staple"

    if _hit(_FRESH_PATTERNS, name):
        return "fresh"
    return "other"


# ── 汇总 ──


def aggregate(recipes: Iterable[dict]) -> dict:
    """菜谱列表 → 按采买区分组的配料汇总。

    Returns:
        {"groups": [{"key", "label", "items": [...]}], "total": n, "dishes": n}
    """
    merged: dict[str, dict] = {}
    dish_count = 0

    for recipe in recipes:
        dish_count += 1
        title = recipe.get("title") or "未命名"
        for ing in recipe.get("ingredients") or []:
            raw_name, raw_amount = split_inverted(
                str(ing.get("name") or ""), str(ing.get("amount") or "")
            )
            raw_name, raw_amount = strip_leading_qty(raw_name, raw_amount)
            name, extra_note = normalize_name(raw_name)
            if _is_noise(name):
                continue
            name = _ALIASES.get(name, name)

            item = merged.get(name)
            if item is None:
                item = merged[name] = {
                    "name": name,
                    "units": {},          # 规范单位 → [下限, 上限]
                    "texts": [],          # 无法求和的用量原文
                    "dishes": [],
                    "notes": [],
                    "optional": True,     # 所有出处都标可选，才算真可选
                    "uncertain": False,
                }

            if title not in item["dishes"]:
                item["dishes"].append(title)

            note = " ".join(x for x in [extra_note, str(ing.get("note") or "").strip()] if x)
            if note and note not in item["notes"]:
                item["notes"].append(note)
            if "可选" not in note:
                item["optional"] = False

            amount = raw_amount.strip()
            qty, uncertain = parse_amount(amount)
            item["uncertain"] = item["uncertain"] or uncertain
            for unit, (lo, hi) in qty.items():
                acc = item["units"].setdefault(unit, [0.0, 0.0])
                acc[0] += lo
                acc[1] += hi
            if not qty and _keep_text_amount(amount) and amount not in item["texts"]:
                item["texts"].append(amount)

    buckets: dict[str, list[dict]] = {key: [] for key, _ in GROUPS}
    for item in merged.values():
        parts = [format_quantity(u, lo, hi) for u, (lo, hi) in sorted(item["units"].items())]
        parts.extend(item["texts"])
        buckets[classify(item["name"])].append({
            "name": item["name"],
            "amount": " + ".join(parts),
            "dishes": item["dishes"],
            "note": "；".join(item["notes"]),
            "optional": item["optional"],
            # 用量依赖变量或压根没写数字时，求和结果只是参考值
            "uncertain": item["uncertain"],
        })

    groups = []
    total = 0
    for key, label in GROUPS:
        items = buckets[key]
        if not items:
            continue
        # 多道菜共用的排前面：那是最该记住别忘买的
        items.sort(key=lambda i: (-len(i["dishes"]), i["name"]))
        groups.append({"key": key, "label": label, "items": items})
        total += len(items)

    return {"groups": groups, "total": total, "dishes": dish_count}
