"""AI 小镇的纯函数测试（不碰 Mongo、不碰网络）。

守的是几条「错了不报错」的边界：

1. **前缀缓存**：同一个人在选地点和写日记两次调用里，system 消息必须逐字相同。
   哪天有人把日期之类会变的东西塞进 system，缓存整段失效 —— 不报错，只是贵几十倍。
2. **关键词激活**：二级关键词逻辑、递归、正则键。激活错了，人物会少一块设定
   或多出一块无关设定，读剧情时根本看不出来。
3. **机关条目排除 + 手动覆盖**：MVU/EJS 一旦漏进去，模型开始输出状态栏；
   误伤了人物设定，又必须能手动开回来。
4. **地点匹配**：模型给的地点名对不上时要落回家里并留警告，不能抛异常让整天失败。
"""

from app.features.town import lore as L
from app.features.town import prompts as P
from app.features.town.engine import cost_of, match_place


def _e(uid, content, *, keys=(), secondary=(), logic=0, constant=False, comment="", order=100, **kw):
    return {"uid": uid, "comment": comment, "content": content, "keys": list(keys),
            "secondary": list(secondary), "logic": logic, "constant": constant,
            "enabled": True, "order": order, "exclude_recursion": False,
            "prevent_recursion": False, "excluded": L.system_reason(comment, content), **kw}


def _res(name="甲", entries=(), rid="r1"):
    return {"_id": rid, "name": name, "kind": "card", "file": "x.png", "home": "home-1",
            "fields": {"description": f"{name}是个剑客。", "personality": "", "scenario": "",
                       "mes_example": "", "first_mes": ""},
            "entries": list(entries), "overrides": {}, "note": ""}


PLACES = [
    {"id": "home-1", "name": "一号屋", "kind": "home", "desc": ""},
    {"id": "home-2", "name": "二号屋", "kind": "home", "desc": ""},
    {"id": "school", "name": "学校", "kind": "public", "desc": ""},
    {"id": "dungeon", "name": "地下城", "kind": "public", "desc": ""},
]


# ── 前缀缓存 ──


def test_system_prefix_identical_across_calls_of_one_resident():
    r = _res(entries=[_e("1", "常驻设定", constant=True), _e("2", "关键词设定", keys=["剑"])])
    plan = P.plan_messages(r, day=3, places=PLACES, facts={}, diaries=[(1, "第一天"), (2, "第二天")],
                           home_name="一号屋")
    diary = P.diary_messages(r, day=3, place=PLACES[2], scene={"title": "t", "body": "他拔出了剑"},
                             others=[], diaries=[(1, "第一天"), (2, "第二天")], target_chars=(200, 500))
    assert plan[0]["role"] == diary[0]["role"] == "system"
    assert plan[0]["content"] == diary[0]["content"]
    # 会变的东西都在 user 里
    assert "第 3 天" not in plan[0]["content"]
    assert "第二天" not in plan[0]["content"]


def test_scene_system_is_concatenation_in_member_order():
    a, b = _res("甲", rid="a"), _res("乙", rid="b")
    msgs = P.scene_messages([a, b], day=1, place=PLACES[2], facts=[], plans={}, memories={},
                            home_owner=None, target_chars=(1000, 2000))
    sys = msgs[0]["content"]
    assert sys.startswith(P.COMMON_HEADER)
    assert sys.index("人物设定：甲") < sys.index("人物设定：乙")


def test_keyed_entries_never_enter_system():
    r = _res(entries=[_e("2", "只在提到剑时出现", keys=["剑"])])
    assert "只在提到剑时出现" not in P.system_for([r])
    msgs = P.diary_messages(r, day=1, place=PLACES[2], scene={"title": "", "body": "他拔出了剑"},
                            others=[], diaries=[], target_chars=(200, 500))
    assert "只在提到剑时出现" in msgs[1]["content"]


# ── 关键词激活 ──


def test_activate_basic_and_case_insensitive():
    es = [_e("1", "A", keys=["Sword"]), _e("2", "B", keys=["盾"])]
    assert [e["uid"] for e in L.activate(es, "a sword here")] == ["1"]


def test_secondary_logic():
    base = dict(keys=["剑"], secondary=["火", "冰"])
    text = "剑上有火"
    assert L.activate([_e("1", "x", logic=L.LOGIC_AND_ANY, **base)], text)
    assert not L.activate([_e("1", "x", logic=L.LOGIC_AND_ALL, **base)], text)
    assert L.activate([_e("1", "x", logic=L.LOGIC_NOT_ALL, **base)], text)
    assert not L.activate([_e("1", "x", logic=L.LOGIC_NOT_ANY, **base)], text)


def test_recursion_pulls_second_level_but_respects_exclude():
    es = [
        _e("1", "提到了阿青", keys=["剑"]),
        _e("2", "阿青的详细设定", keys=["阿青"]),
        _e("3", "不该被递归点亮", keys=["阿青"], exclude_recursion=True),
    ]
    got = {e["uid"] for e in L.activate(es, "剑")}
    assert got == {"1", "2"}


def test_regex_key():
    assert L.activate([_e("1", "x", keys=["/第[0-9]+层/"])], "到了第3层")
    assert not L.activate([_e("1", "x", keys=["/第[0-9]+层/"])], "到了第三层")


def test_no_keys_never_activates():
    assert not L.activate([_e("1", "x")], "任何内容")


# ── 机关条目 ──


def test_system_reason_catches_machinery():
    assert L.system_reason("[mvu_update]变量更新规则", "随便")
    assert L.system_reason("", "<% if (x) { %>hi<% } %>")
    assert L.system_reason("", "当前好感 {{getvar::好感}}")
    assert L.system_reason("", "<StatusPlaceHolderImpl/>")
    assert L.system_reason("", "   ")


def test_system_reason_keeps_plain_character_lore():
    assert L.system_reason("人物：阿青", "阿青，十七岁，越女剑传人，性子冷。") is None


def test_override_beats_auto_exclusion():
    e = _e("1", "<% x %>真正的人设", constant=True)
    assert not L.is_enabled(e, {})
    assert L.is_enabled(e, {"1": "on"})
    plain = _e("2", "普通设定", constant=True)
    assert not L.is_enabled(plain, {"2": "off"})


def test_render_profile_uses_overrides_and_note():
    r = _res(entries=[_e("1", "保留", constant=True), _e("2", "<% 机关 %>", constant=True)])
    r["note"] = "小镇里他二十五岁"
    text = L.render_profile(r)
    assert "保留" in text and "机关" not in text and "二十五岁" in text
    r["overrides"] = {"2": "on", "1": "off"}
    text = L.render_profile(r)
    assert "机关" in text and "保留" not in text


# ── 宏 / 清洗 ──


def test_macros():
    s = L.apply_macros("{{char}}看着{{user}}。{{// 作者注释 }}<USER>", "阿青", "原作主角")
    assert s == "阿青看着原作主角。原作主角"


def test_clean_prose_strips_html():
    s = L.clean_prose("<style>.a{}</style><div>你好</div>\n```html\n<b>x</b>\n```\n再见")
    assert "style" not in s and "<" not in s and "你好" in s and "再见" in s


# ── 地点匹配 ──


def test_match_place():
    assert match_place("学校", PLACES, "home-1") == ("school", None)
    assert match_place("学校（去看看）", PLACES, "home-1") == ("school", None)
    assert match_place("家", PLACES, "home-2") == ("home-2", None)
    assert match_place("地下城入口", PLACES, "home-1")[0] == "dungeon"
    pid, warn = match_place("月球", PLACES, "home-1")
    assert pid == "home-1" and warn
    pid, warn = match_place("", PLACES, "home-1")
    assert pid == "home-1" and warn


# ── 计费 ──


def test_cost_counts_cache_hits_cheaply():
    c = cost_of({"prompt_tokens": 100_000, "prompt_cache_hit_tokens": 90_000,
                 "prompt_cache_miss_tokens": 10_000, "completion_tokens": 1000})
    assert c["cache_hit"] == 90_000 and c["input"] == 100_000 and c["calls"] == 1
    no_cache = cost_of({"prompt_tokens": 100_000, "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 100_000, "completion_tokens": 1000})
    assert c["yuan"] < no_cache["yuan"] / 3


def test_cost_without_miss_field_derives_it():
    c = cost_of({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 0})
    full = cost_of({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400,
                    "prompt_cache_miss_tokens": 600, "completion_tokens": 0})
    assert c["yuan"] == full["yuan"]
