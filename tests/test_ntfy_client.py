"""ntfy 客户端的认证头。

自建 ntfy 开了 deny-all 之后，漏带令牌不会让推送「报错退出」，只会让每一条
通知都变成 403 —— 被调用方 catch 住只记一行日志，手机上就是悄无声息。
"""

from __future__ import annotations

from app.clients import ntfy


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"id": "x"}


def _capture(monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(ntfy.requests, "post", fake_post)
    return seen


def test_有令牌时带_Bearer_头(monkeypatch):
    seen = _capture(monkeypatch)
    ntfy.NtfyClient("https://ntfy.example.com/", "p_", "tk_abc").send("spider", "t", "m", priority="low")
    assert seen["headers"] == {"Authorization": "Bearer tk_abc"}
    assert seen["url"] == "https://ntfy.example.com"
    assert seen["json"]["topic"] == "p_spider"


def test_没令牌时不带认证头_公共ntfy_sh照旧(monkeypatch):
    seen = _capture(monkeypatch)
    ntfy.NtfyClient("https://ntfy.sh", "p_").send("spider", "t", "m", priority="low")
    assert not seen["headers"]
