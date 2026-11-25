"""Offline tests for daily_report's overview_metrics.json helpers (task #92)."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import daily_plan
import daily_report
from warehouse_routing import REGULAR_WAREHOUSE_ID, SMALL_WAREHOUSE_ID

TR = timezone(timedelta(hours=3))
RATE = 40.0

MS_A = "MS-T61008800T-BLACK-36"     # Pajama
MS_B = "MS-T81006840V-PEBBLE-38"    # Socks
MS_C = "MS-T33000001-NAVY-M"        # article not in products.csv -> Unknown
HM_1 = "HM-12345-S"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no live Ozon call allowed in tests")
    monkeypatch.setattr(daily_report, "call", boom)
    monkeypatch.setattr(daily_report, "fetch_usd_try_rate", lambda: RATE)
    monkeypatch.setattr(daily_report, "list_active_campaigns", lambda: [])
    monkeypatch.setattr(daily_report, "list_enrolled_products", lambda cid: [])


def _item(offer_id, qty, price, cur="USD"):
    return {"offer_id": offer_id, "quantity": qty, "price": str(price), "currency_code": cur}


def _posting(products, status="delivered", wid=None):
    p = {"status": status, "products": products}
    if wid is not None:
        p["delivery_method"] = {"warehouse_id": wid}
    return p


LABELS = {"T61008800T": "Pajama", "T81006840V": "Socks"}


def _sample():
    return [
        # FBS posting shipped from the small warehouse: wins over assignments.
        _posting([_item(MS_A, 2, 20.0), _item(MS_B, 1, 10.0)], wid=SMALL_WAREHOUSE_ID),
        # No posting warehouse -> assignments decide.
        _posting([_item(MS_A, 1, 20.0)]),
        _posting([_item(HM_1, 1, 30.0)]),
        # Unknown category, no warehouse anywhere.
        _posting([_item(MS_C, 1, 5.0)]),
        # Cancelled: excluded entirely.
        _posting([_item(MS_A, 5, 20.0)], status="cancelled", wid=REGULAR_WAREHOUSE_ID),
        # Only a non-USD item: skipped, not an order.
        _posting([_item(MS_B, 3, 700.0, cur="RUB")]),
    ]


def test_summarize_sales_counts_and_splits():
    assignments = {MS_A: REGULAR_WAREHOUSE_ID, MS_B: REGULAR_WAREHOUSE_ID,
                   HM_1: str(SMALL_WAREHOUSE_ID)}
    ms_prices = {MS_A: (368.0, "u"), MS_B: (200.0, "u")}  # A: 9.2$/20 = 46%, B: 5$/10 = 50%
    s = daily_report.summarize_sales(_sample(), ms_prices, RATE, assignments, LABELS)

    assert s["orders"] == 4
    assert s["units"] == 6
    assert s["skus"] == 4
    assert s["revenue_usd"] == pytest.approx(2 * 20 + 10 + 20 + 30 + 5)
    assert s["non_usd_skipped"] == 3
    # quantity-weighted M&S cost: (46*3 + 50*1) / 4 = 47.0
    assert s["avg_ms_cost_pct"] == pytest.approx(47.0)

    wh = s["warehouse_pct"]
    # small: posting 1 (3 units) + HM via assignments (1) = 4; regular: MS_A via
    # assignments (1); unknown: MS_C (1).
    assert wh == {"small": 66.7, "regular": 16.7, "unknown": 16.6}
    assert sum(wh.values()) == pytest.approx(100, abs=0.1)

    br = s["brand_pct"]
    assert br == {"M&S": 83.3, "H&M": 16.7, "Other": 0.0}
    assert sum(br.values()) == pytest.approx(100, abs=0.1)

    assert s["top_category"] == {"label": "Pajama", "units": 3}


def test_posting_warehouse_beats_assignments():
    postings = [_posting([_item(MS_A, 1, 20.0)], wid=REGULAR_WAREHOUSE_ID)]
    s = daily_report.summarize_sales(postings, {}, RATE, {MS_A: SMALL_WAREHOUSE_ID}, LABELS)
    assert s["warehouse_pct"] == {"small": 0.0, "regular": 100.0, "unknown": 0.0}


def test_foreign_posting_warehouse_falls_back_to_assignments():
    postings = [_posting([_item(MS_A, 1, 20.0)], wid=123)]
    s = daily_report.summarize_sales(postings, {}, RATE, {MS_A: SMALL_WAREHOUSE_ID}, LABELS)
    assert s["warehouse_pct"]["small"] == 100.0


def test_unknown_category_only_top_when_nothing_else():
    postings = [_posting([_item(MS_C, 5, 5.0), _item(MS_B, 1, 10.0)])]
    s = daily_report.summarize_sales(postings, {}, RATE, {}, LABELS)
    assert s["top_category"] == {"label": "Socks", "units": 1}

    s = daily_report.summarize_sales([_posting([_item(MS_C, 2, 5.0)])], {}, RATE, {}, {})
    assert s["top_category"] == {"label": "Unknown", "units": 2}


def test_summarize_sales_empty():
    for postings in ([], None, [_posting([_item(MS_A, 1, 20.0)], status="cancelled")]):
        s = daily_report.summarize_sales(postings, {}, None, {}, {})
        assert s["orders"] == 0 and s["units"] == 0 and s["skus"] == 0
        assert s["revenue_usd"] == 0
        assert s["avg_ms_cost_pct"] is None
        assert s["top_category"] is None
        assert s["warehouse_pct"] == {"small": 0.0, "regular": 0.0, "unknown": 0.0}
        assert s["brand_pct"] == {"M&S": 0.0, "H&M": 0.0, "Other": 0.0}
        assert s["window_from"] is None and s["window_to"] is None


def test_sales_window_34h():
    now = datetime(2026, 11, 25, 10, 0, tzinfo=TR)
    frm, to = daily_report.sales_window_34h(now)
    assert frm == datetime(2026, 11, 24, 0, 0, tzinfo=TR)
    assert frm.astimezone(timezone.utc) == datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)
    assert to == now
    assert (to - frm) == timedelta(hours=34)


def test_get_sales_34h_queries_utc_window(monkeypatch):
    seen = []

    def fake_fbs(a, b):
        seen.append((a, b))
        return [_posting([_item(MS_A, 1, 20.0)], wid=SMALL_WAREHOUSE_ID)]

    monkeypatch.setattr(daily_report, "fetch_fbs_postings", fake_fbs)
    monkeypatch.setattr(daily_report, "fetch_fbo_postings", lambda a, b: [])
    monkeypatch.setattr(daily_report, "load_warehouse_assignments", lambda: {})
    monkeypatch.setattr(daily_plan, "_article_labels", lambda path: LABELS)
    now = datetime(2026, 11, 25, 10, 0, tzinfo=TR)
    s = daily_report.get_sales_34h({}, RATE, now=now)
    a, b = seen[0]
    assert a.utcoffset() == timedelta(0)
    assert a.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-11-23T21:00:00Z"
    assert b.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-11-25T07:00:00Z"
    assert s["window_from"] == "2026-11-24T00:00:00+03:00"
    assert s["top_category"] == {"label": "Pajama", "units": 1}


def test_campaign_metrics(monkeypatch):
    monkeypatch.setattr(daily_report, "list_active_campaigns", lambda: [{"id": 1}, {"id": 2}])
    enrolled = {
        1: [{"id": 11, "action_price": 50.0},   # 920/40 = 23$ / 50 = 46.0% (inclusive)
            {"id": 12, "action_price": 40.0}],  # 12$ / 40 = 30%
        2: [{"id": 13, "action_price": 30.0}],  # no M&S cost -> live_skus only
    }
    monkeypatch.setattr(daily_report, "list_enrolled_products", lambda cid: enrolled[cid])

    def fake_call(path, params):
        assert path == "/v3/product/info/list"
        return {"items": [{"id": 11, "offer_id": "MS-A"}, {"id": 12, "offer_id": "MS-B"},
                          {"id": 13, "offer_id": "HM-1"}]}

    monkeypatch.setattr(daily_report, "call", fake_call)
    ms_prices = {"MS-A": (920.0, "u"), "MS-B": (480.0, "u")}
    m = daily_report.get_campaign_metrics(daily_report.fetch_usd_try_rate(), ms_prices)
    assert m["live_skus"] == 3
    assert m["ratio_count"] == 2
    assert m["avg_ratio_pct"] == 38.0
    assert m["min_ratio_pct"] == 30.0
    assert m["max_ratio_pct"] == 46.0
    stats = daily_report.get_campaign_stats(RATE, ms_prices)
    assert stats == (3, 38.0)
    count, avg = stats
    assert (count, avg) == (3, 38.0)
    assert daily_report.campaign_metrics_from_stats(stats) == m
    assert daily_report.campaign_metrics_from_stats((5, 30.0))["live_skus"] == 5


def test_campaign_metrics_empty():
    m = daily_report.get_campaign_metrics(RATE, {})
    assert m == {"live_skus": 0, "ratio_count": 0, "avg_ratio_pct": None,
                 "min_ratio_pct": None, "max_ratio_pct": None}


def test_write_overview_metrics_atomic(tmp_path, monkeypatch):
    path = tmp_path / "overview_metrics.json"
    path.write_text("old", encoding="utf-8")
    campaigns = daily_report.get_campaign_metrics(RATE, {})
    sales = daily_report.summarize_sales([], {}, RATE, {}, {})
    replaced = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((src, dst))
        assert os.path.exists(src)
        real_replace(src, dst)

    monkeypatch.setattr(daily_report.os, "replace", spy_replace)
    daily_report.write_overview_metrics(campaigns, sales, path=str(path),
                                        now=datetime(2026, 11, 25, 10, 0, tzinfo=TR))
    assert len(replaced) == 1 and replaced[0][1] == str(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "built_at", "campaigns", "sales"}
    assert data["version"] == 1
    assert data["built_at"].startswith("2026-11-25T")
    assert set(data["campaigns"]) == {"live_skus", "ratio_count", "avg_ratio_pct",
                                      "min_ratio_pct", "max_ratio_pct"}
    assert set(data["sales"]) == {"window_from", "window_to", "revenue_usd", "orders", "units",
                                  "skus", "avg_ms_cost_pct", "warehouse_pct", "brand_pct",
                                  "top_category", "non_usd_skipped"}
    assert list(tmp_path.iterdir()) == [path]


def test_write_overview_metrics_failure_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "overview_metrics.json"
    path.write_text('{"version": 1}', encoding="utf-8")

    def bad_replace(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(daily_report.os, "replace", bad_replace)
    with pytest.raises(OSError):
        daily_report.write_overview_metrics({}, None, path=str(path))
    assert path.read_text(encoding="utf-8") == '{"version": 1}'
    assert list(tmp_path.iterdir()) == [path]


def _patch_gather(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_report, "load_ms_prices", lambda: {})
    monkeypatch.setattr(daily_report, "get_products_added_today", lambda: 1)
    monkeypatch.setattr(daily_report, "get_stock_freshness", lambda: (1, 0, 1))
    monkeypatch.setattr(daily_report, "check_quota", lambda: None)
    monkeypatch.setattr(daily_report, "get_catalog_margin_flagged_count", lambda: 0)
    monkeypatch.setattr(daily_report, "get_warehouse_counts", lambda: (0, 0))
    monkeypatch.setattr(daily_report, "get_sales_window", lambda a, b: {
        "units_sold": 0, "revenue_usd": 0.0, "avg_ms_cost_pct": None,
        "ms_sold_over_46_count": 0, "non_ms_excluded_count": 0, "non_usd_skipped_count": 0})
    monkeypatch.setattr(daily_report, "fetch_fbo_postings", lambda a, b: [])
    monkeypatch.setattr(daily_report, "load_warehouse_assignments", lambda: {})
    monkeypatch.setattr(daily_plan, "_article_labels", lambda path: {})
    monkeypatch.setattr(daily_report, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(daily_report, "RUN_STATUS_FILE", tmp_path / "daily_run_status.json")
    monkeypatch.setattr(daily_report.telegram_notify, "send_telegram", lambda text, **k: True)


def test_main_writes_metrics_before_github_post(monkeypatch, tmp_path):
    _patch_gather(monkeypatch, tmp_path)
    monkeypatch.setattr(daily_report, "fetch_fbs_postings",
                        lambda a, b: [_posting([_item(MS_A, 1, 20.0)])])

    def gh_fails(body):
        raise RuntimeError("gh down")

    monkeypatch.setattr(daily_report, "post_github_report", gh_fails)
    with pytest.raises(RuntimeError):
        daily_report.main()
    data = json.loads((tmp_path / "overview_metrics.json").read_text(encoding="utf-8"))
    assert data["campaigns"]["live_skus"] == 0
    assert data["sales"]["units"] == 1


def test_sales_fetch_failure_still_reports(monkeypatch, tmp_path):
    _patch_gather(monkeypatch, tmp_path)
    posted = []

    def fbs_fails(a, b):
        raise RuntimeError("posting list down")

    monkeypatch.setattr(daily_report, "fetch_fbs_postings", fbs_fails)
    monkeypatch.setattr(daily_report, "post_github_report", lambda body: posted.append(body))
    daily_report.main()
    assert len(posted) == 1
    data = json.loads((tmp_path / "overview_metrics.json").read_text(encoding="utf-8"))
    assert data["sales"] is None
