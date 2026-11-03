"""媒体下载：来源输入识别 + 新条目分档 + 「检测不覆盖已有状态」。

    .venv/bin/pytest tests/test_media_rules.py -v
"""

from __future__ import annotations

import pytest

from app.features.media import rules as R


# ── 输入识别 ─────────────────────────────────────────────────────────────

def test_wx_username_recognised():
    p = R.parse_source_input("wx", "  v2_060000231003b20faec8c4e48e1dc3ddcd03ec3cb077bb11b3c6c9a42ee3cea8073a64e6e2bd@finder ")
    assert p.key.endswith("@finder") and not p.error


def test_wx_nickname_kept_unresolved():
    p = R.parse_source_input("wx", "我不叫龙虾.")
    assert p.key == "" and p.input == "我不叫龙虾." and not p.error


def test_wx_rejects_links():
    assert R.parse_source_input("wx", "https://weixin.qq.com/sph/AbC").error


@pytest.mark.parametrize("raw,lid", [
    ("https://www.youtube.com/watch?v=0rB8vx8jBEM&list=PLCBTT_1tcrlUxyz", "PLCBTT_1tcrlUxyz"),
    ("https://www.youtube.com/playlist?list=PLabc-DEF_123", "PLabc-DEF_123"),
    ("youtube.com/playlist?list=PLabc", "PLabc"),
    ("https://youtu.be/0rB8vx8jBEM?list=PLq", "PLq"),
    ("https://m.youtube.com/watch?list=PLm&v=x&index=3", "PLm"),
    ("PLCBTT_1tcrlU", "PLCBTT_1tcrlU"),
    ("LL", "LL"),
])
def test_yt_list_id_extracted(raw, lid):
    p = R.parse_source_input("yt", raw)
    assert not p.error and p.key == lid
    assert p.input == f"https://www.youtube.com/playlist?list={lid}"


def test_yt_without_list_rejected():
    assert R.parse_source_input("yt", "https://www.youtube.com/watch?v=0rB8vx8jBEM").error


def test_yt_mix_rejected():
    """RD… 是 YouTube 自动生成的合辑，每次打开内容都不同，当下载清单会无限冒新条目。"""
    assert R.parse_source_input("yt", "https://www.youtube.com/watch?v=x&list=RDx").error


# ── 新条目分档 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,baseline,expected", [
    ("all", False, "pending"),
    ("all", True, "pending"),
    ("new_only", False, "skipped"),   # 首次检测：作者的历史作品不进队列
    ("new_only", True, "pending"),    # 之后出现的才是「更新」
])
def test_initial_status(mode, baseline, expected):
    assert R.initial_status(mode, baseline) == expected


# ── 昵称匹配 ─────────────────────────────────────────────────────────────

def test_pick_exact_only_when_unique():
    cands = [{"nickname": "我不叫龙虾.", "username": "a"}, {"nickname": "辣个龙虾", "username": "b"}]
    assert R.pick_exact_candidate("我不叫龙虾.", cands)["username"] == "a"
    assert R.pick_exact_candidate("龙虾", cands) is None, "模糊匹配不能自动选，会静默盯错人"
    dup = cands + [{"nickname": "我不叫龙虾.", "username": "c"}]
    assert R.pick_exact_candidate("我不叫龙虾.", dup) is None


# ── 检测不改已有状态（repo 的 upsert_found 用假集合跑）────────────────────

class _FakeResult:
    def __init__(self, upserted_id):
        self.upserted_id = upserted_id


class _FakeItems:
    """只实现 upsert_found 用到的 update_one(filter, {$set, $setOnInsert}, upsert=True)。"""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def update_one(self, flt, upd, upsert=False):
        _id = flt["_id"]
        if _id in self.docs:
            self.docs[_id].update(upd.get("$set", {}))
            return _FakeResult(None)
        doc = {"_id": _id, **upd.get("$setOnInsert", {}), **upd.get("$set", {})}
        self.docs[_id] = doc
        return _FakeResult(_id)


def _repo_with_fake():
    from app.features.media.repo import MediaRepo
    repo = MediaRepo.__new__(MediaRepo)
    repo.items = _FakeItems()
    return repo


def test_rescan_never_resets_status():
    repo = _repo_with_fake()
    src = {"_id": "yt:PLx", "platform": "yt", "name": "收藏"}
    found = [{"key": "v1", "title": "t1"}, {"key": "v2", "title": "t2"}]
    assert repo.upsert_found(src, found, "pending") == 2
    repo.items.docs["yt:v1"]["status"] = "done"
    repo.items.docs["yt:v2"]["status"] = "skipped"
    # 再检测一次（比如标题改了）：只刷元数据，状态不动，也不算新增
    n = repo.upsert_found(src, [{"key": "v1", "title": "t1-new"}, {"key": "v2", "title": "t2"},
                                {"key": "v3", "title": "t3"}], "pending")
    assert n == 1
    assert repo.items.docs["yt:v1"]["status"] == "done"
    assert repo.items.docs["yt:v1"]["title"] == "t1-new"
    assert repo.items.docs["yt:v2"]["status"] == "skipped"
    assert repo.items.docs["yt:v3"]["status"] == "pending"


def test_upsert_ignores_items_without_key():
    repo = _repo_with_fake()
    assert repo.upsert_found({"_id": "wx:a", "platform": "wx"}, [{"key": ""}, {}], "pending") == 0
