"""聊天记录「哪份正开着」的锁：settings.json 的 active_character + 卡的 chat 指针。

不碰真实酒馆：在 tmp_path 里搭一个最小的 data/<user>/ 目录。
"""

import base64
import json
import os
import struct
import zlib

import pytest

from app.core.settings import settings
from app.features.tavern.catalog import tavern_catalog
from app.features.tavern.chat_ops import TavernChatOps, TavernOpError
from app.features.tavern.chats import TavernChatError, tavern_chats


def _png(card: dict) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    text = b"chara\x00" + base64.b64encode(json.dumps(card).encode())
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", text) + chunk(b"IEND", b""))


@pytest.fixture
def st(tmp_path, monkeypatch):
    user = tmp_path / "default-user"
    (user / "characters").mkdir(parents=True)
    monkeypatch.setattr(settings, "tavern_data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tavern_user", "default-user")
    tavern_chats.invalidate()
    tavern_catalog._cache = None

    class Env:
        def card(self, png, name, chat, chats=()):
            (user / "characters" / png).write_bytes(_png({"name": name, "chat": chat}))
            d = user / "chats" / png[:-4]
            d.mkdir(parents=True, exist_ok=True)
            for c in chats:
                (d / f"{c}.jsonl").write_text('{"chat_metadata":{}}\n')
            tavern_catalog._cache = None

        def select(self, **kw):
            (user / "settings.json").write_text(json.dumps(kw))

        def path(self, png, chat):
            return user / "chats" / png[:-4] / f"{chat}.jsonl"

    yield Env()
    tavern_chats.invalidate()
    tavern_catalog._cache = None


def test_only_the_open_chat_of_the_active_card_is_locked(st):
    st.card("a.png", "A", "a2", chats=["a1", "a2"])
    st.card("b.png", "B", "b1", chats=["b1"])
    st.select(active_character="a.png", active_group=None)

    ops = TavernChatOps()
    with pytest.raises(TavernOpError, match="正在酒馆里开着"):
        ops._guard_active(st.path("a.png", "a2"))
    ops._guard_active(st.path("a.png", "a1"))   # 同卡的另一份：放行
    ops._guard_active(st.path("b.png", "b1"))   # 别的卡的默认会话：放行（它只是书签）


def test_group_or_nothing_selected_locks_nothing(st):
    st.card("a.png", "A", "a1", chats=["a1"])
    st.select(active_character=None, active_group="g1")
    assert tavern_chats.open_chat() is None
    st.select()
    assert tavern_chats.open_chat() is None


def test_unreadable_settings_refuses_instead_of_allowing(st):
    st.card("a.png", "A", "a1", chats=["a1"])
    # 没有 settings.json：判断不了 → 不放行
    with pytest.raises(TavernChatError):
        TavernChatOps()._guard_active(st.path("a.png", "a1"))
    st.select(active_character="3")   # 老版本的数字下标，认不出就别猜
    with pytest.raises(TavernChatError):
        tavern_chats.open_chat()


def test_overview_reports_open_without_failing(st):
    st.card("a.png", "A", "a1", chats=["a1"])
    st.select(active_character="a.png")
    assert tavern_chats.overview()["open"] == {"dir": "a", "chat": "a1"}
    (st.path("a.png", "a1").parents[2] / "settings.json").unlink()
    out = tavern_chats.overview()
    assert out["open"] is None and out["open_error"]


class _FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, path, payload):
        self.calls.append((path, payload))
        return {}


def _ops():
    ops = TavernChatOps()
    ops.http = _FakeHttp()
    return ops


def test_deleting_default_chat_repoints_to_most_recent_remaining(st):
    st.card("a.png", "A", "a1", chats=["a1", "a2", "a3"])
    os.utime(st.path("a.png", "a2"), (1, 2_000_000_000))
    os.utime(st.path("a.png", "a3"), (1, 1_000_000_000))
    st.path("a.png", "a1").unlink()          # 模拟酒馆已删掉文件
    ops = _ops()
    ops._repoint_after("A", "a.png", "a1", None)
    assert ops.http.calls == [("/api/characters/merge-attributes",
                               {"avatar": "a.png", "chat": "a2"})]


def test_deleting_last_chat_gets_fresh_name_never_empty(st):
    st.card("a.png", "A", "a1", chats=["a1"])
    st.path("a.png", "a1").unlink()
    ops = _ops()
    ops._repoint_after("A", "a.png", "a1", None)
    (_, payload), = ops.http.calls
    assert payload["chat"].startswith("A - ") and payload["chat"].endswith("ms")


def test_repoint_only_when_pointer_matches(st):
    st.card("a.png", "A", "a1", chats=["a1", "a2"])
    ops = _ops()
    ops._repoint_after("A", "a.png", "a2", "renamed")   # 动的不是默认会话
    assert ops.http.calls == []
    ops._repoint_after("A", "a.png", "a1", "renamed")
    assert ops.http.calls[0][1] == {"avatar": "a.png", "chat": "renamed"}


def test_repoint_failure_does_not_fail_the_operation(st, monkeypatch):
    st.card("a.png", "A", "a1", chats=["a1"])
    ops = TavernChatOps()

    def boom(*a, **k):
        raise TavernOpError("down")
    monkeypatch.setattr(ops.http, "post", boom)
    ops._repoint_after("A", "a.png", "a1", "x")   # 不抛
