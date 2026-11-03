"""统一 LLM 用量（llm_usage / llm_pricing）的规格 —— 只测纯函数，不碰 Mongo。

最怕的都是「不报错、只给错数字」的失效：
  - 半行推进了水位 → 永远少一笔
  - cache write 漏算 → Claude 那部分的未命中被低估
  - 峰谷按德国时间判 → 每天有几个小时价格错一倍
  - DeepSeek 自己的调用被开关强行换价 → 真实账单被假设覆盖

    .venv/bin/pytest tests/test_llm_usage.py -v
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.features.llm_usage.router import month_range
from app.features.llm_usage.pricing import deepseek_tokens, pricing_model, summarize
from app.features.llm_usage.service import (
    drop_cross_source_duplicates,
    ledger_row_to_doc,
    liyuan_record_to_doc,
    split_complete_lines,
)

UTC = dt.timezone.utc
TZ_DE = ZoneInfo("Europe/Berlin")

# 2026-11-02 是周一。UTC 02:00 落在峰时 01-04；UTC 12:00 是谷时。
PEAK = dt.datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
OFF = dt.datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
WEEKEND_PEAK_HOUR = dt.datetime(2026, 11, 7, 2, 0, tzinfo=UTC)  # 周六，同一钟点也是谷时


def _row(**kw):
    base = {"v": 1, "source": "dsh", "id": "a", "ts": int(PEAK.timestamp() * 1000),
            "provider": "claude", "model": "claude-opus", "miss": 10, "hit": 1000,
            "cacheWrite": 90, "output": 50, "reasoning": 5}
    base.update(kw)
    return base


def _doc(ts=PEAK, provider="claude", model="claude-opus", source="dsh",
         miss=1_000_000, hit=0, cache_write=0, output=0):
    return {"ts": ts, "provider": provider, "model": model, "source": source,
            "miss": miss, "hit": hit, "cache_write": cache_write, "output": output}


# ── 账本行映射 ──


class TestLedgerRow:
    def test_basic_mapping(self):
        d = ledger_row_to_doc(_row(), "dsh")
        assert d["id"] == "a"
        assert d["ts"] == PEAK
        assert (d["miss"], d["hit"], d["cache_write"], d["output"]) == (10, 1000, 90, 50)
        assert d["peak"] is True

    def test_source_falls_back_to_directory(self):
        row = _row()
        del row["source"]
        assert ledger_row_to_doc(row, "tavern")["source"] == "tavern"

    def test_null_tokens_survive_as_none(self):
        d = ledger_row_to_doc(_row(miss=None, hit=None, output=None, cacheWrite=None), "dsh")
        assert d["miss"] is None and d["output"] is None

    @pytest.mark.parametrize("bad", [{"v": 2}, {"id": ""}, {"ts": "x"}, {"id": None}])
    def test_unknown_or_broken_rows_are_skipped(self, bad):
        assert ledger_row_to_doc(_row(**bad), "dsh") is None

    def test_backfill_flag(self):
        assert ledger_row_to_doc(_row(src="backfill"), "dsh")["backfill"] is True


class TestSplitLines:
    def test_partial_tail_not_consumed(self):
        lines, used = split_complete_lines(b'{"a":1}\n{"b":2}\n{"c":')
        assert lines == [b'{"a":1}', b'{"b":2}']
        assert used == len(b'{"a":1}\n{"b":2}\n')

    def test_no_newline_consumes_nothing(self):
        assert split_complete_lines(b'{"half"') == ([], 0)

    def test_blank_lines_ignored(self):
        lines, used = split_complete_lines(b'\n{"a":1}\n\n')
        assert lines == [b'{"a":1}'] and used == 10


class TestCrossSource:
    def test_tavern_row_with_liyuan_id_dropped(self):
        docs = [{"id": "r1", "source": "tavern"}, {"id": "r2", "source": "tavern"}, {"id": "r1", "source": "dsh"}]
        kept = drop_cross_source_duplicates(docs, {"r1"})
        assert [(d["id"], d["source"]) for d in kept] == [("r2", "tavern"), ("r1", "dsh")]

    def test_liyuan_doc_uses_response_id(self):
        rec = SimpleNamespace(response_id="resp-9", session_id="s", seq=3, ts=OFF, model="deepseek-flash",
                              miss=1, hit=2, cache_write=0, output=3, reasoning=0)
        d = liyuan_record_to_doc(rec)
        assert d["id"] == "resp-9" and d["source"] == "liyuan" and d["provider"] == "deepseek"
        assert d["peak"] is False
        rec.response_id = ""
        assert liyuan_record_to_doc(rec)["id"] == "liyuan:s:3"


# ── 计价 ──


class TestPricingInputs:
    def test_cache_write_counts_as_miss(self):
        assert deepseek_tokens({"miss": 10, "hit": 1000, "cache_write": 90, "output": 50}) == (100, 1000, 50)

    def test_missing_tokens_not_billable(self):
        assert deepseek_tokens({"miss": None, "output": None}) is None

    def test_non_deepseek_uses_target(self):
        assert pricing_model({"provider": "claude", "model": "x"}, "deepseek-v4-pro") == "deepseek-v4-pro"

    def test_deepseek_keeps_its_real_model(self):
        doc = {"provider": "deepseek", "model": "deepseek-v4-flash"}
        assert pricing_model(doc, "deepseek-v4-pro") == "deepseek-flash"  # 旧名归一

    def test_force_overrides_real_model(self):
        doc = {"provider": "deepseek-official", "model": "deepseek-flash"}
        assert pricing_model(doc, "deepseek-v4-pro", force=True) == "deepseek-v4-pro"


class TestSummarize:
    # 官方峰价（USD / 1M）：flash miss 0.30 / hit 0.006 / out 1.20；pro miss 1.32 / hit 0.044 / out 3.96；谷价减半。

    def test_flash_peak_vs_offpeak(self):
        peak = summarize([_doc(ts=PEAK)], "deepseek-flash")
        off = summarize([_doc(ts=OFF)], "deepseek-flash")
        assert peak["total"]["usd"] == pytest.approx(0.30)
        assert off["total"]["usd"] == pytest.approx(0.15)

    def test_weekend_is_offpeak_even_in_peak_hour(self):
        assert summarize([_doc(ts=WEEKEND_PEAK_HOUR)], "deepseek-flash")["total"]["usd"] == pytest.approx(0.15)

    def test_pro_target(self):
        doc = _doc(ts=PEAK, miss=1_000_000, hit=1_000_000, output=1_000_000)
        assert summarize([doc], "deepseek-v4-pro")["total"]["usd"] == pytest.approx(1.32 + 0.044 + 3.96)

    def test_switch_changes_only_non_deepseek(self):
        docs = [_doc(source="dsh"), _doc(source="tavern", provider="deepseek", model="deepseek-flash")]
        flash = summarize(docs, "deepseek-flash")
        pro = summarize(docs, "deepseek-v4-pro")
        by = lambda s: {b["source"]: b["usd"] for b in s["by_source"]}  # noqa: E731
        assert by(flash)["tavern"] == pytest.approx(by(pro)["tavern"]) == pytest.approx(0.30)
        assert by(pro)["dsh"] == pytest.approx(1.32)
        assert summarize(docs, "deepseek-v4-pro", force=True)["total"]["usd"] == pytest.approx(2.64)

    def test_composition_percentages(self):
        doc = _doc(ts=PEAK, miss=1_000_000, hit=1_000_000, output=1_000_000)
        comp = summarize([doc], "deepseek-flash")["total"]["composition"]
        total = 0.30 + 0.006 + 1.20
        assert comp["output"]["pct"] == pytest.approx(1.20 / total * 100)
        assert sum(c["pct"] for c in comp.values()) == pytest.approx(100)
        assert comp["hit"]["tokens"] == 1_000_000

    def test_unbillable_rows_count_as_requests_only(self):
        s = summarize([_doc(), {**_doc(), "miss": None, "output": None}], "deepseek-flash")
        assert s["total"]["requests"] == 2
        assert s["total"]["priced_requests"] == 1

    def test_all_sources_always_listed_with_shares(self):
        s = summarize([_doc(source="dsh")], "deepseek-flash")
        assert [b["source"] for b in s["by_source"]] == ["dsh", "dsh-deepseek", "tavern", "liyuan"]
        assert s["by_source"][0]["share_pct"] == pytest.approx(100)
        assert s["by_source"][1]["usd"] == 0

    def test_dsh_split_by_provider(self):
        docs = [_doc(source="dsh"), _doc(source="dsh", provider="deepseek-official", model="deepseek-flash")]
        rows = {b["source"]: b for b in summarize(docs, "deepseek-v4-pro")["by_source"]}
        assert rows["dsh"]["usd"] == pytest.approx(1.32)          # Claude 部分按开关估算
        assert rows["dsh-deepseek"]["usd"] == pytest.approx(0.30)  # DeepSeek 部分按实际模型
        assert rows["dsh"]["share_pct"] + rows["dsh-deepseek"]["share_pct"] == pytest.approx(100)

    def test_hit_rate_counts_cache_write_as_miss(self):
        doc = _doc(miss=10, hit=900, cache_write=90, output=0)
        assert summarize([doc], "deepseek-flash")["total"]["hit_rate"] == pytest.approx(90.0)
        assert summarize([], "deepseek-flash")["total"]["hit_rate"] is None

    def test_empty_month(self):
        s = summarize([], "deepseek-flash")
        assert s["total"]["usd"] == 0
        assert all(c["pct"] == 0 for c in s["total"]["composition"].values())

    def test_bad_target_rejected(self):
        with pytest.raises(ValueError):
            summarize([], "gpt-5")


# ── 月份划界（德国时间）──


class TestMonthRange:
    def test_bounds_follow_berlin(self):
        month, s, e = month_range("2026-11")
        assert month == "2026-11"
        assert s == dt.datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # CET = UTC+1
        assert e == dt.datetime(2026, 11, 30, 23, 0, tzinfo=UTC)

    def test_default_is_current_berlin_month(self):
        # 德国 12-01 00:30 已是 12 月，而 UTC 还在 11-30
        now = dt.datetime(2026, 11, 30, 23, 30, tzinfo=UTC)
        assert month_range(None, now)[0] == "2026-12"

    def test_december_rolls_year(self):
        _, _, e = month_range("2026-12")
        assert e.astimezone(TZ_DE).date() == dt.date(2027, 1, 1)

    @pytest.mark.parametrize("bad", ["2026-13", "2026-1", "nov"])
    def test_bad_month(self, bad):
        with pytest.raises(ValueError):
            month_range(bad)


# ── 按项目归并 ──

from app.features.llm_usage.projects import (
    UNCATEGORIZED,
    UNKNOWN_PROJECT,
    Place,
    category_of,
    place_of,
    summarize_projects,
)

MACHINES = {"100.64.0.11-dev-22": "work-pc", "100.64.0.22-root-2200": "3060ti-wsl",
            "100.64.0.11": "work-pc", "100.64.0.22": "3060ti-wsl"}


class TestPlace:
    def test_local_path_uses_last_folder(self):
        assert place_of("/root/myspiders", MACHINES) == Place("myspiders", None)
        assert place_of("/mnt/c/Users/x/Desktop/test/TG视频上传工具/", MACHINES) == Place("TG视频上传工具", None)

    def test_remote_path_maps_machine_name(self):
        p = place_of("/root/.dsh/remote-workspaces/100.64.0.11-dev-22/CampaignManagementAPI", MACHINES)
        assert p == Place("CampaignManagementAPI", "work-pc")

    def test_remote_root_and_unknown_machine(self):
        assert place_of("/root/.dsh/remote-workspaces/100.64.0.11-dev-22", MACHINES) == Place("work-pc 根目录", "work-pc")
        assert place_of("/root/.dsh/remote-workspaces/1.2.3.4-bob-22/x/proj", MACHINES) == Place("proj", "1.2.3.4")

    def test_unsaved_user_port_falls_back_to_host(self):
        p = place_of("/root/.dsh/remote-workspaces/100.64.0.22-Administrator-22/TG视频上传工具", MACHINES)
        assert p == Place("TG视频上传工具", "3060ti-wsl")

    def test_load_machines_reads_dsh_remote_file(self, tmp_path):
        from app.features.llm_usage.projects import load_machines
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"list": [{"name": "w", "host": "1.1.1.1", "username": "root", "port": 2200}]}))
        assert load_machines(f) == {"1.1.1.1-root-2200": "w", "1.1.1.1": "w"}
        assert load_machines(tmp_path / "missing.json") == {}

    def test_missing_cwd(self):
        assert place_of(None, MACHINES).project == UNKNOWN_PROJECT

    def test_category_project_beats_machine(self):
        cats = [("工作", {"machines": ["3060ti-wsl"]}), ("游戏 mod", {"projects": ["nsp-mods"]})]
        assert category_of(Place("nsp-mods", "3060ti-wsl"), cats) == "游戏 mod"
        assert category_of(Place("other", "3060ti-wsl"), cats) == "工作"
        assert category_of(Place("other", None), cats) == UNCATEGORIZED


class TestSummarizeProjects:
    def test_same_folder_on_two_machines_merges(self):
        docs = [
            {**_doc(), "session_id": "a"},
            {**_doc(), "session_id": "b"},
            {**_doc(source="tavern", provider="deepseek", model="deepseek-flash"), "session_id": None},
        ]
        cwds = {"a": "/root/nsp-mods", "b": "/root/.dsh/remote-workspaces/100.64.0.22-root-2200/nsp-mods"}
        out = summarize_projects(docs, cwds, "deepseek-flash", machines=MACHINES)
        assert [c["category"] for c in out] == ["游戏 mod"]           # 酒馆来源不进项目视图
        proj = out[0]["projects"][0]
        assert proj["project"] == "nsp-mods" and proj["requests"] == 2
        assert proj["machines"] == ["3060ti-wsl", "本机"]
        assert out[0]["usd"] == pytest.approx(0.60)
        assert proj["share_pct"] == pytest.approx(100)

    def test_unknown_session_still_counted(self):
        out = summarize_projects([{**_doc(), "session_id": "gone"}], {}, "deepseek-flash", machines=MACHINES)
        assert out[0]["category"] == UNCATEGORIZED
        assert out[0]["projects"][0]["project"] == UNKNOWN_PROJECT


# ── 按日汇总 ──

from app.features.llm_usage.pricing import summarize_daily


class TestDaily:
    def test_empty_days_are_kept_as_zero(self):
        """断档必须留在图上：没调用的日子是一行 0，不是省略。"""
        docs = [_doc(ts=dt.datetime(2026, 10, 1, 12, tzinfo=UTC)),
                _doc(ts=dt.datetime(2026, 10, 4, 12, tzinfo=UTC))]
        days = summarize_daily(docs, "deepseek-flash", False,
                               dt.date(2026, 10, 1), dt.date(2026, 10, 5), TZ_DE)
        assert [d["date"] for d in days] == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
        assert [d["requests"] for d in days] == [1, 0, 0, 1, 0]
        assert days[1]["usd"] == 0 and days[1]["hit_rate"] is None and days[1]["by_source"] == {}

    def test_day_boundary_is_german_time(self):
        """UTC 22:30 在德国（夏令时 UTC+2）已经是第二天。"""
        docs = [_doc(ts=dt.datetime(2026, 9, 30, 22, 30, tzinfo=UTC))]
        days = summarize_daily(docs, "deepseek-flash", False,
                               dt.date(2026, 9, 30), dt.date(2026, 10, 1), TZ_DE)
        assert [d["requests"] for d in days] == [0, 1]

    def test_dst_change_day_is_one_row(self):
        """10-25 换冬令时那天有 25 小时，仍然是一行。"""
        docs = [_doc(ts=dt.datetime(2026, 10, 24, 22, 30, tzinfo=UTC)),   # 德国 10-25 00:30 CEST
                _doc(ts=dt.datetime(2026, 10, 25, 22, 30, tzinfo=UTC))]   # 德国 10-25 23:30 CET
        days = summarize_daily(docs, "deepseek-flash", False,
                               dt.date(2026, 10, 25), dt.date(2026, 10, 26), TZ_DE)
        assert [d["requests"] for d in days] == [2, 0]

    def test_days_sum_to_month_summary(self):
        docs = [_doc(ts=PEAK), _doc(ts=OFF, provider="deepseek", model="deepseek-chat", source="tavern"),
                _doc(ts=WEEKEND_PEAK_HOUR, hit=500_000, output=10_000)]
        days = summarize_daily(docs, "deepseek-v4-pro", False,
                               dt.date(2026, 11, 1), dt.date(2026, 11, 30), TZ_DE)
        total = summarize(docs, "deepseek-v4-pro")["total"]["usd"]
        assert sum(d["usd"] for d in days) == pytest.approx(total)
        assert sum(sum(d["by_source"].values()) for d in days) == pytest.approx(total)
        assert set(days[1]["by_source"]) == {"dsh", "tavern"}  # 11-02 两条

    def test_out_of_range_docs_ignored(self):
        docs = [_doc(ts=dt.datetime(2026, 12, 1, 12, tzinfo=UTC))]
        days = summarize_daily(docs, "deepseek-flash", False,
                               dt.date(2026, 11, 1), dt.date(2026, 11, 30), TZ_DE)
        assert sum(d["requests"] for d in days) == 0
