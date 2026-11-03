"""梨园会话解析与期间划界的规格。

解析层的两个核心风险（都不会报错、只会给出错数字）：
  1. 轮末镜像去重 —— 漏了会让总量虚高约 12%
  2. usage.input 是「未命中」而非「完整 prompt」—— 搞错会把命中率算成 0

不碰 Mongo：只测纯解析。（期间划界随旧梨园用量页一起下线，月份划界见 test_llm_usage.py。）

    .venv/bin/pytest tests/test_liyuan_usage.py -v
"""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from app.features.llm_usage.liyuan_usage import parse_session_file

TZ_DE = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc


# ── 构造会话 jsonl 的小工具 ──


def _session_line(sid="sess-1"):
    return {"type": "session", "version": 1, "id": sid,
            "timestamp": "2026-09-24T15:20:00.000Z", "cwd": "/root/liyuan"}


def _model_line(model="deepseek-flash"):
    return {"type": "model_change", "id": "m1",
            "timestamp": "2026-09-24T15:20:01.000Z",
            "provider": "deepseek", "modelId": model}


def _user_line(text, ts="2026-09-24T15:21:00.000Z"):
    return {"type": "message", "id": "u1", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _usage(inp, out, hit=0, cw=0, reasoning=0):
    return {"input": inp, "output": out, "cacheRead": hit, "cacheWrite": cw,
            "reasoning": reasoning, "totalTokens": inp + out + hit,
            "cost": {"total": 0}}


def _process_line(usage, ts="2026-09-24T15:22:00.000Z", idx="p1"):
    """type=custom 的 liyuan-process 记录（usage 在 data.message.usage）。"""
    return {"type": "custom", "customType": "liyuan-process", "id": idx,
            "timestamp": ts,
            "data": {"message": {"role": "assistant", "usage": usage}}}


def _assistant_line(usage, ts="2026-09-24T15:23:00.000Z", idx="a1"):
    """轮末 type=message 记录。"""
    return {"type": "message", "id": idx, "timestamp": ts,
            "message": {"role": "assistant", "usage": usage,
                        "content": [{"type": "text", "text": "正文"}]}}


def _write(tmp_path, rows, workspace="--root-liyuan--", name="s.jsonl"):
    d = tmp_path / workspace
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    return f


# ── 解析 ──


class TestParseSessionFile:
    def test_basic_extraction(self, tmp_path):
        f = _write(tmp_path, [
            _session_line(), _model_line(),
            _user_line("青梅竹马、重逢"),
            _process_line(_usage(15507, 313, hit=0, reasoning=174)),
        ])
        recs = parse_session_file(f)
        assert len(recs) == 1
        r = recs[0]
        assert r.miss == 15507
        assert r.hit == 0
        assert r.output == 313
        assert r.reasoning == 174
        assert r.session_id == "sess-1"
        assert r.turn == 1
        assert r.user_excerpt == "青梅竹马、重逢"
        assert r.model == "deepseek-flash"

    def test_mirror_at_turn_end_is_deduped(self, tmp_path):
        """核心坑①：轮末 assistant 记录是最后一次 process 的原样复制。"""
        u = _usage(194, 90, hit=28160, reasoning=50)
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("下一幕"),
            _process_line(_usage(2690, 291, hit=14976), idx="p1"),
            _process_line(u, idx="p2", ts="2026-09-24T15:24:00.000Z"),
            _assistant_line(u, idx="a1", ts="2026-09-24T15:24:05.000Z"),  # 镜像
        ])
        recs = parse_session_file(f)
        assert len(recs) == 2, "镜像记录应被去重"
        assert [r.miss for r in recs] == [2690, 194]

    def test_identical_usage_in_different_turns_not_deduped(self, tmp_path):
        """去重只看「紧邻的上一条」；隔着别的记录的相同 usage 必须保留。"""
        same = _usage(100, 50, hit=1000)
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("一"),
            _process_line(same, idx="p1"),
            _process_line(_usage(999, 20, hit=2000), idx="p2"),
            _process_line(same, idx="p3"),  # 与 p1 相同但不相邻
        ])
        recs = parse_session_file(f)
        assert len(recs) == 3

    def test_turn_counting_across_multiple_user_messages(self, tmp_path):
        f = _write(tmp_path, [
            _session_line(), _model_line(),
            _user_line("第一轮"),
            _process_line(_usage(100, 10), idx="p1"),
            _user_line("第二轮", ts="2026-09-24T15:30:00.000Z"),
            _process_line(_usage(200, 20), idx="p2",
                          ts="2026-09-24T15:31:00.000Z"),
        ])
        recs = parse_session_file(f)
        assert [r.turn for r in recs] == [1, 2]
        assert recs[1].user_excerpt == "第二轮"

    def test_seq_is_monotonic_from_one(self, tmp_path):
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("x"),
            _process_line(_usage(1, 1), idx="p1"),
            _process_line(_usage(2, 2), idx="p2"),
            _process_line(_usage(3, 3), idx="p3"),
        ])
        assert [r.seq for r in parse_session_file(f)] == [1, 2, 3]

    def test_prompt_total_is_miss_plus_hit(self, tmp_path):
        """核心坑②：input 只是未命中部分，完整 prompt 要加上 cacheRead。"""
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("x"),
            _process_line(_usage(144, 107, hit=25344)),
        ])
        doc = parse_session_file(f)[0].to_doc()
        assert doc["prompt_total"] == 144 + 25344

    def test_malformed_line_is_skipped(self, tmp_path):
        d = tmp_path / "--root-liyuan--"
        d.mkdir(parents=True)
        f = d / "s.jsonl"
        f.write_text("\n".join([
            json.dumps(_session_line()),
            "{ this is not json",
            json.dumps(_model_line()),
            json.dumps(_process_line(_usage(10, 5))),
        ]), encoding="utf-8")
        recs = parse_session_file(f)
        assert len(recs) == 1

    def test_record_without_timestamp_is_skipped(self, tmp_path):
        bad = _process_line(_usage(10, 5))
        del bad["timestamp"]
        f = _write(tmp_path, [_session_line(), _model_line(), bad])
        assert parse_session_file(f) == []

    def test_empty_file(self, tmp_path):
        f = _write(tmp_path, [])
        assert parse_session_file(f) == []

    def test_file_with_no_usage_records(self, tmp_path):
        f = _write(tmp_path, [_session_line(), _model_line(), _user_line("hi")])
        assert parse_session_file(f) == []

    def test_legacy_model_name_is_canonicalized(self, tmp_path):
        f = _write(tmp_path, [
            _session_line(), _model_line("deepseek-v4-flash"),
            _user_line("x"), _process_line(_usage(1000, 100)),
        ])
        r = parse_session_file(f)[0]
        assert r.model == "deepseek-flash"
        assert r.priced is True

    def test_model_change_midsession_is_tracked(self, tmp_path):
        f = _write(tmp_path, [
            _session_line(), _model_line("deepseek-flash"),
            _user_line("x"), _process_line(_usage(10, 5), idx="p1"),
            _model_line("deepseek-v4-pro"),
            _process_line(_usage(20, 5), idx="p2"),
        ])
        recs = parse_session_file(f)
        assert recs[0].model == "deepseek-flash"
        assert recs[1].model == "deepseek-v4-pro"

    def test_cost_is_recomputed_not_taken_from_jsonl(self, tmp_path):
        """jsonl 里 cost 恒为 0（本地代理没配价），必须自己算出非零金额。"""
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("x"),
            _process_line(_usage(1_000_000, 0)),
        ])
        r = parse_session_file(f)[0]
        assert r.cost_total > 0

    def test_workspace_from_parent_dir(self, tmp_path):
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("x"),
            _process_line(_usage(10, 5)),
        ], workspace="稿件")
        assert parse_session_file(f)[0].workspace == "稿件"

    def test_string_content_user_message(self, tmp_path):
        """content 可能是裸字符串而非分块列表。"""
        line = {"type": "message", "id": "u1",
                "timestamp": "2026-09-24T15:21:00.000Z",
                "message": {"role": "user", "content": "纯字符串输入"}}
        f = _write(tmp_path, [
            _session_line(), _model_line(), line, _process_line(_usage(10, 5)),
        ])
        assert parse_session_file(f)[0].user_excerpt == "纯字符串输入"

    def test_peak_flag_frozen_at_parse_time(self, tmp_path):
        """UTC 15:22 → 谷时；标记必须随记录一起冻结。"""
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("x"),
            _process_line(_usage(10, 5), ts="2026-09-24T15:22:00.000Z"),
        ])
        assert parse_session_file(f)[0].peak is False

    def test_peak_flag_true_in_peak_window(self, tmp_path):
        """2026-09-24 是周四，UTC 08:00 落在 06-10 峰窗。"""
        f = _write(tmp_path, [
            _session_line(), _model_line(), _user_line("x"),
            _process_line(_usage(10, 5), ts="2026-09-24T08:00:00.000Z"),
        ])
        r = parse_session_file(f)[0]
        assert r.peak is True
        assert r.to_doc()["saved_if_offpeak"] == pytest.approx(r.cost_total / 2)
