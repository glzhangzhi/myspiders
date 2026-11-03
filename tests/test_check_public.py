"""发布前扫描器 scripts/check_public.py 的正反例。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_public", ROOT / "scripts" / "check_public.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


def _rules(text: str) -> set[str]:
    return {h.split("[", 1)[1].split("]", 1)[0] for h in cp.scan_text("x", text)}


def test_flags_real_looking_secrets_and_pii():
    assert "IBAN" in _rules("IBAN: DE12500105170648489890")
    assert "Tailscale/CGNAT IP" in _rules("host 100.101.102.103")
    assert "局域网 IP" in _rules("kindle 192.168.178.28")
    assert "公网 IP" in _rules("server 130.61.21.42")
    assert "Slack token" in _rules("token xoxp-1234567890-abcdef")
    assert "OpenAI/DeepSeek key" in _rules("sk-" + "a" * 32)
    assert "GitHub token" in _rules("ghp_" + "A" * 36)
    assert "私钥" in _rules("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert "Telegram 邀请链接" in _rules("https://t.me/+AbCdEfGh123")


def test_allows_documented_placeholders_and_versions():
    assert not _rules("Chrome/154.0.0.0 Safari")
    assert not _rules("IBAN: DE00100110010000000001")
    assert not _rules("example 203.0.113.10")
    assert not _rules("version 1.2.3.4")
    assert not _rules("loopback 127.0.0.1 and 10.0.0.1")
