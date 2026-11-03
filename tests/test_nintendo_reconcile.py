"""Nintendo：一次抓取并进已有记录的规则（app/features/nintendo/reconcile.py）。

数据源只有「特价」列表，「不在列表里」= 折扣结束。最危险的失效是**抓取不完整时
把缺的那截全判成结束** —— 一次错杀几百条且不报错，所以 listed=None 时绝不判结束。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.features.nintendo.models import NinPriceRecord
from app.features.nintendo.reconcile import Scraped, reconcile

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=10)


def rec(pid, cur=50.0, orig=100.0, **kw):
    return NinPriceRecord(pid=pid, title=pid, original_price=orig, current_price=cur,
                          discount_percent=round((orig - cur) / orig * 100, 2), updated_at=T0, **kw)


def by_pid(changed):
    return {r.pid: r for r in changed}


def test_new_game_is_event_and_on_sale():
    changed, ev = reconcile({}, [Scraped("a", "A", 100, 40)], NOW, {"a"})
    r = by_pid(changed)["a"]
    assert r.on_sale and r.sale_started_at == NOW and r.discount_percent == 60
    assert [e.kind for e in ev] == ["new"]


def test_missing_from_complete_list_means_ended_and_keeps_updated_at():
    changed, ev = reconcile({"a": rec("a")}, [], NOW, set())
    r = by_pid(changed)["a"]
    assert not r.on_sale and r.sale_ended_at == NOW
    assert r.updated_at == T0, "结束不应把它顶到「按更新时间」最前面"
    assert ev == []


def test_incomplete_scrape_never_ends_anything():
    changed, _ = reconcile({"a": rec("a")}, [], NOW, None)
    assert "a" not in by_pid(changed)


def test_listed_but_unparsable_is_not_ended():
    changed, _ = reconcile({"a": rec("a")}, [], NOW, {"a"})
    assert "a" not in by_pid(changed)


def test_resale_after_end_is_event():
    old = rec("a", on_sale=False, sale_ended_at=T0)
    changed, ev = reconcile({"a": old}, [Scraped("a", "A", 100, 50)], NOW, {"a"})
    r = by_pid(changed)["a"]
    assert r.on_sale and r.sale_started_at == NOW and r.updated_at == NOW and r.sale_ended_at is None
    assert [e.kind for e in ev] == ["resale"]


def test_short_flap_same_price_is_silent_restore():
    old = rec("a", on_sale=False, sale_ended_at=NOW - timedelta(days=1))
    changed, ev = reconcile({"a": old}, [Scraped("a", "A", 100, 50)], NOW, {"a"})
    r = by_pid(changed)["a"]
    assert r.on_sale and r.updated_at == T0 and ev == []


def test_cheaper_is_event_pricier_is_silent_update():
    changed, ev = reconcile({"a": rec("a", cur=50), "b": rec("b", cur=50)},
                            [Scraped("a", "A", 100, 30), Scraped("b", "B", 100, 70)], NOW, {"a", "b"})
    c = by_pid(changed)
    assert c["a"].current_price == 30 and c["b"].current_price == 70
    assert c["b"].updated_at == NOW
    assert [e.record.pid for e in ev] == ["a"]


def test_unchanged_only_touches_last_seen():
    changed, ev = reconcile({"a": rec("a")}, [Scraped("a", "A", 100, 50)], NOW, {"a"})
    r = by_pid(changed)["a"]
    assert r.last_seen_at == NOW and r.updated_at == T0 and ev == []


def test_duplicate_across_pages_counted_once():
    changed, ev = reconcile({}, [Scraped("a", "A", 100, 40)] * 2, NOW, {"a"})
    assert len(changed) == 1 and len(ev) == 1


def test_legacy_record_without_field_reads_as_on_sale():
    r = NinPriceRecord(pid="x", title="x", original_price=1, current_price=1, discount_percent=0)
    assert r.on_sale is True
