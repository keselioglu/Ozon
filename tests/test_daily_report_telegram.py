import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json

import pytest

import daily_report

TODAY = "2026-11-24"

SNAPSHOT = {
    "date": TODAY,
    "products_added": 312,
    "stock_freshness_str": "900 up to date / 3 unknown",
    "quota_left": 1688,
    "campaign_str": "400 in campaigns / 903 total live",
    "units_sold": 7,
    "revenue_usd": 123.456,
}


def _status(state="completed", steps=None, **kw):
    st = {
        "date": TODAY,
        "state": state,
        "started_at": "2026-11-24T04:00:01",
        "finished_at": None if state == "running" else "2026-11-24T05:42:10",
        "steps": steps if steps is not None else [
            {"name": "Upload", "script": "upload_to_ozon.py", "status": "ok", "seconds": 1.0}],
        "summary": "2026-11-24: ok",
        "attention": [],
        "created_today": 200,
        "plan_total": 500,
        "content_summary": "content: avg 63.2, 120 groups improved",
        "upload_totals": None,
    }
    st.update(kw)
    return st


def _check_basic(text):
    assert len(text) < 1000
    assert "<" not in text and "*" not in text  # plain text, no markup
    assert len(text.splitlines()) <= 10
    assert text.splitlines()[0] == f"Ozon daily report {TODAY}"


def test_completed_no_problems():
    text = daily_report.build_telegram_text(SNAPSHOT, _status(), TODAY)
    _check_basic(text)
    assert "Daily run: ✅ completed 05:42" in text
    assert "Problems:" not in text
    assert "New offers: 312/500 plan (quota left 1688)" in text
    assert "Content: avg 63.2, 120 groups improved" in text
    assert "Stock: 900 up to date / 3 unknown" in text
    assert "Campaigns: 400 in campaigns / 903 total live" in text
    assert "Sales 24h: 7 units, 123.46 USD" in text


def test_completed_with_failed_and_timeout_steps():
    steps = [
        {"name": "Upload", "status": "failed"},
        {"name": "Crawl", "status": "ok"},
        {"name": "Content rating", "status": "timeout"},
    ]
    text = daily_report.build_telegram_text(SNAPSHOT, _status(steps=steps), TODAY)
    _check_basic(text)
    assert "Daily run: ⚠️ completed with 2 problems" in text
    assert "Problems: Upload (failed), Content rating (timeout)" in text
    assert "Crawl" not in text


def test_problems_capped_at_five():
    steps = [{"name": f"Step {i}", "status": "failed"} for i in range(8)]
    text = daily_report.build_telegram_text(SNAPSHOT, _status(steps=steps), TODAY)
    _check_basic(text)
    assert "Step 4 (failed), +3 more" in text
    assert "Step 5" not in text


def test_failed_state():
    st = _status(state="failed", attention=["products.csv could not be opened"],
                 steps=[{"name": "Crawl", "status": "failed"}])
    text = daily_report.build_telegram_text(SNAPSHOT, st, TODAY)
    _check_basic(text)
    assert "Daily run: ❌ FAILED: products.csv could not be opened" in text
    assert "Problems: Crawl (failed)" in text


def test_status_missing_uses_plan_file(monkeypatch):
    import daily_plan
    monkeypatch.setattr(daily_plan, "load_plan", lambda *a, **k: daily_plan.default_plan())
    text = daily_report.build_telegram_text(SNAPSHOT, None, TODAY)
    _check_basic(text)
    assert "Daily run: ❓ no run today (last status: none)" in text
    assert "New offers: 312/500 plan" in text
    assert "Content status unknown" in text


def test_status_old_date(monkeypatch):
    import daily_plan
    monkeypatch.setattr(daily_plan, "load_plan", lambda *a, **k: daily_plan.default_plan())
    st = _status(date="2026-10-12", plan_total=123)
    text = daily_report.build_telegram_text(SNAPSHOT, st, TODAY)
    _check_basic(text)
    assert "Daily run: ❓ no run today (last status: 2026-10-12)" in text
    assert "New offers: 312/500 plan" in text  # stale plan_total ignored
    assert "Content status unknown" in text


def test_running_state():
    text = daily_report.build_telegram_text(SNAPSHOT, _status(state="running", steps=[]), TODAY)
    _check_basic(text)
    assert "Daily run: ⏳ still running or crashed (started 04:00)" in text


def test_snapshot_values_none():
    snap = {k: None for k in SNAPSHOT}
    text = daily_report.build_telegram_text(snap, _status(content_summary=None), TODAY)
    _check_basic(text)
    assert "New offers: ?/500 plan (quota left ?)" in text
    assert "Stock: ?" in text and "Campaigns: ?" in text
    assert "Sales 24h: ? units, ? USD" in text
    assert "Content status unknown" in text
    # Totally broken inputs still give text, never raise.
    assert daily_report.build_telegram_text(None, {"date": TODAY, "steps": "x"}, TODAY)


def test_load_run_status(monkeypatch, tmp_path):
    path = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_report, "RUN_STATUS_FILE", path)
    assert daily_report.load_run_status(TODAY) is None
    path.write_text("{not json", encoding="utf-8")
    assert daily_report.load_run_status(TODAY) is None
    path.write_text(json.dumps(_status()), encoding="utf-8")
    assert daily_report.load_run_status(TODAY)["state"] == "completed"


def _patch_main(monkeypatch, tmp_path, sent, posted):
    monkeypatch.setattr(daily_report, "fetch_usd_try_rate", lambda: 34.0)
    monkeypatch.setattr(daily_report, "load_ms_prices", lambda: {})
    monkeypatch.setattr(daily_report, "get_products_added_today", lambda: 12)
    monkeypatch.setattr(daily_report, "get_stock_freshness", lambda: (10, 1, 11))
    monkeypatch.setattr(daily_report, "check_quota",
                        lambda: {"daily_create": {"limit": 2000, "usage": 12}})
    monkeypatch.setattr(daily_report, "get_campaign_stats", lambda r, p: (5, 30.0))
    monkeypatch.setattr(daily_report, "get_catalog_margin_flagged_count", lambda: 0)
    monkeypatch.setattr(daily_report, "get_warehouse_counts", lambda: (3, 8))
    monkeypatch.setattr(daily_report, "get_sales_window", lambda a, b: {
        "units_sold": 2, "revenue_usd": 40.0, "avg_ms_cost_pct": None,
        "ms_sold_over_46_count": 0, "non_ms_excluded_count": 0, "non_usd_skipped_count": 0})
    monkeypatch.setattr(daily_report, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(daily_report, "RUN_STATUS_FILE", tmp_path / "daily_run_status.json")
    monkeypatch.setattr(daily_report, "post_github_report", lambda body: posted.append(body))
    monkeypatch.setattr(daily_report.telegram_notify, "send_telegram",
                        lambda text, **k: sent.append(text) or True)


def test_main_sends_telegram_once(monkeypatch, tmp_path):
    sent, posted = [], []
    _patch_main(monkeypatch, tmp_path, sent, posted)
    daily_report.main()
    assert len(posted) == 1
    assert len(sent) == 1
    assert "New offers: 12/" in sent[0]
    assert "quota left 1988" in sent[0]
    assert "no run today" in sent[0]
    assert (tmp_path / "history.jsonl").exists()


def test_main_metric_crash_still_sends_and_reraises(monkeypatch, tmp_path):
    sent, posted = [], []
    _patch_main(monkeypatch, tmp_path, sent, posted)

    def boom():
        raise ValueError("network down")

    monkeypatch.setattr(daily_report, "get_stock_freshness", boom)
    with pytest.raises(ValueError):
        daily_report.main()
    assert posted == []
    assert len(sent) == 1
    assert "Daily report failed: ValueError" in sent[0]
    assert "Daily run:" in sent[0]
