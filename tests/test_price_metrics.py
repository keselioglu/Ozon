"""Run metrics written by refresh_prices.py (price_refresh_history.jsonl) and
daily_price_update.py (price_changed_ok / limit_changed_ok in
price_update_history.jsonl). Offline: Ozon calls and M&S page fetches are faked."""
import json
import sys
import types

import pytest

try:
    import set_margin_floor_prices  # noqa: F401  (untracked helper on the live PC)
except ImportError:  # not in this checkout: stub it, the test fakes fetch_all_prices anyway
    stub = types.ModuleType("set_margin_floor_prices")
    stub.fetch_all_prices = lambda: []
    sys.modules["set_margin_floor_prices"] = stub

import daily_price_update
import refresh_prices


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- refresh_prices

URL_OK = "https://ms/ok"          # 2 offers, price 100 (one was 90 -> changed)
URL_ERR = "https://ms/error"      # fetch raises -> failed
URL_EMPTY = "https://ms/empty"    # no product data -> failed
URL_SAME = "https://ms/same"      # 1 offer, price unchanged

URL_FOR = {"MS-A-1": URL_OK, "MS-A-2": URL_OK, "MS-B-1": URL_ERR, "MS-C-1": URL_EMPTY, "MS-D-1": URL_SAME}


def _fake_extract(url):
    if url == URL_ERR:
        raise RuntimeError("connection reset")
    if url == URL_EMPTY:
        return []
    price = 100.0 if url == URL_OK else 50.0
    return [{"ms_article_code": "X", "parent_sku": "Y", "price": price, "currency": "TRY"}]


@pytest.fixture
def rp_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(refresh_prices, "fetch_live_ms_identifiers", lambda: set(URL_FOR))
    monkeypatch.setattr(refresh_prices, "load_legacy_url_map", lambda: {})
    monkeypatch.setattr(refresh_prices, "load_pipeline_url_map", lambda live: dict(URL_FOR))
    monkeypatch.setattr(refresh_prices, "fetch_live_offer_ids_matching",
                        lambda ids: (sorted(ids), 10))
    monkeypatch.setattr(refresh_prices, "extract_product", _fake_extract)
    monkeypatch.setattr(refresh_prices, "verify_url_matches_offer_id", lambda oid, a, p: True)
    monkeypatch.setattr(refresh_prices.time, "sleep", lambda s: None)
    with open(tmp_path / refresh_prices.PRICE_HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps({"date": "2026-11-01", "offer_id": "MS-A-1", "url": URL_OK,
                            "price": 90.0, "currency": "TRY"}) + "\n")
        f.write(json.dumps({"date": "2026-11-01", "offer_id": "MS-D-1", "url": URL_SAME,
                            "price": 50.0, "currency": "TRY"}) + "\n")
    return tmp_path


def test_refresh_prices_appends_one_summary_line(rp_env):
    refresh_prices.main()
    recs = _read_jsonl(rp_env / refresh_prices.PRICE_REFRESH_HISTORY_FILE)
    assert len(recs) == 1
    r = recs[0]
    assert r["urls_checked"] == 4
    assert r["urls_failed"] == 2
    assert r["prices_checked"] == 3
    assert r["prices_changed"] == 1
    assert r["date"] and r["finished_at"]
    # Existing outputs still written as before.
    changes = json.loads((rp_env / refresh_prices.PRICE_CHANGES_FILE).read_text(encoding="utf-8"))
    assert [c["offer_id"] for c in changes["changes"]] == ["MS-A-1"]
    assert len(_read_jsonl(rp_env / refresh_prices.PRICE_HISTORY_FILE)) == 2 + 3

    refresh_prices.main()  # a second run appends, never overwrites
    assert len(_read_jsonl(rp_env / refresh_prices.PRICE_REFRESH_HISTORY_FILE)) == 2


def test_refresh_prices_nothing_to_check_still_records(rp_env, monkeypatch):
    monkeypatch.setattr(refresh_prices, "fetch_live_offer_ids_matching", lambda ids: ([], 0))
    refresh_prices.main()
    recs = _read_jsonl(rp_env / refresh_prices.PRICE_REFRESH_HISTORY_FILE)
    assert len(recs) == 1
    assert {k: recs[0][k] for k in ("urls_checked", "urls_failed", "prices_checked", "prices_changed")} == \
        {"urls_checked": 0, "urls_failed": 0, "prices_checked": 0, "prices_changed": 0}


# ---------------------------------------------------------------- daily_price_update

RATE = 40.0
COST_TRY = 400.0       # -> $10 cost; first price 4x = 40.00
LIMIT = 20.84          # ceil(10 / 0.48 * 100) / 100

# offer_id -> (current Ozon price, current min_price)
CURRENT = {
    "MS-SAME-1": ("40.004", f"{LIMIT}"),   # nothing changed (float noise ignored)
    "MS-PRICE-1": ("50", f"{LIMIT}"),      # price changed only
    "MS-LIMIT-1": ("40", "15"),            # limit changed only
    "MS-FAIL-1": ("50", "15"),             # both changed, Ozon rejects it
    "MS-MISSING-1": ("50", "15"),          # both changed, absent from Ozon's response
    "MS-BOTH-1": ("50", "15"),             # both changed, accepted
}

OLD_KEYS = {"date", "finished_at", "usd_try", "fresh_share", "on_ladder", "needed_update",
            "pushed_ok", "push_failed", "new_at_4x", "drop_to_3x", "drop_to_2_7x", "drop_to_2_43x",
            "sold_clock_reset", "waiting", "held_at_campaign_limit", "skipped_no_ms_price",
            "skipped_non_usd"}


@pytest.fixture
def dpu_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    today = daily_price_update.date.today().isoformat()
    ladder = {oid: {"first_price": 40.0, "first_date": today, "stage": 0, "stage_since": today,
                    "in_stock_days": 0, "counted_on": today} for oid in CURRENT}
    (tmp_path / daily_price_update.LADDER_FILE).write_text(json.dumps(ladder), encoding="utf-8")

    live = [{"offer_id": oid, "price": {"currency_code": "USD", "price": p, "old_price": "40",
                                         "min_price": m}} for oid, (p, m) in CURRENT.items()]
    monkeypatch.setattr(daily_price_update, "fetch_usd_try_rate", lambda: RATE)
    monkeypatch.setattr(daily_price_update, "load_ms_prices",
                        lambda: {oid: (COST_TRY, "2026-11-25") for oid in CURRENT})
    monkeypatch.setattr(daily_price_update, "todays_price_offer_ids", lambda: set(CURRENT))
    monkeypatch.setattr(daily_price_update, "fetch_all_prices", lambda: live)
    monkeypatch.setattr(daily_price_update, "get_real_stock", lambda ids: {oid: 1 for oid in ids})
    monkeypatch.setattr(daily_price_update, "last_sale_dates", lambda: {})

    sent = []

    def fake_call(endpoint, payload=None):
        assert endpoint == "/v1/product/import/prices", endpoint
        sent.extend(payload["prices"])
        result = []
        for u in payload["prices"]:
            if u["offer_id"] == "MS-MISSING-1":
                continue
            if u["offer_id"] == "MS-FAIL-1":
                result.append({"offer_id": u["offer_id"], "updated": False,
                               "errors": [{"message": "rejected"}]})
            else:
                result.append({"offer_id": u["offer_id"], "updated": True, "errors": []})
        return {"result": result}

    monkeypatch.setattr(daily_price_update, "call", fake_call)
    monkeypatch.setattr(sys, "argv", ["daily_price_update.py", "--apply"])
    return tmp_path, sent


def test_daily_price_update_counts_only_accepted_real_changes(dpu_env, capsys):
    tmp_path, sent = dpu_env
    assert daily_price_update.main() == 0

    # Every offer is still pushed, with unchanged payloads.
    assert sorted(u["offer_id"] for u in sent) == sorted(CURRENT)
    for u in sent:
        assert u == {"offer_id": u["offer_id"], "price": "40.00", "old_price": "40.00",
                     "min_price": f"{LIMIT:.2f}", "currency_code": "USD"}

    recs = _read_jsonl(tmp_path / daily_price_update.HISTORY_FILE)
    assert len(recs) == 1
    rec = recs[0]
    assert OLD_KEYS <= set(rec)
    assert rec["needed_update"] == 6
    assert rec["pushed_ok"] == 4 and rec["push_failed"] == 1
    assert rec["price_changed_ok"] == 2   # MS-PRICE-1, MS-BOTH-1
    assert rec["limit_changed_ok"] == 2   # MS-LIMIT-1, MS-BOTH-1
    assert "2 price(s) changed, 2 campaign limit(s) changed" in capsys.readouterr().out


def test_daily_price_update_dry_run_writes_no_history(dpu_env, monkeypatch):
    tmp_path, sent = dpu_env
    monkeypatch.setattr(sys, "argv", ["daily_price_update.py"])
    assert daily_price_update.main() == 0
    assert sent == []
    assert not (tmp_path / daily_price_update.HISTORY_FILE).exists()
