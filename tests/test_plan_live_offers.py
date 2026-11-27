"""Dry run 2026-11-27 at 1000/day: the cached H&M inventory (built the day before) still counted
the ~800 offers uploaded that day, so 'urgent' underwear took the whole day and the run found
539 offers. Products already on Ozon must be left out of the plan."""
import json

import product_rules as pr
import run_new_products as rnp


def _repo(tmp_path, new_today=()):
    inv = {"candidates": [
        {"article": "1000000001", "label": "Underwear", "offers": 600, "price_try": 900, "title": "a"},
        {"article": "1000000002", "label": "Underwear", "offers": 300, "price_try": 900, "title": "b"}] + [
        {"article": f"20000000{i:02d}", "label": "Dress", "offers": 50, "price_try": 900, "title": "c"}
        for i in range(12)]}
    (tmp_path / "hm_inventory.json").write_text(json.dumps(inv), encoding="utf-8")
    (tmp_path / "products.csv").write_text("url,name,price,stock_count\n", encoding="utf-8")
    (tmp_path / "new_items_submitted.json").write_text(
        json.dumps({"date": "2026-11-27", "new_offer_ids": list(new_today)}), encoding="utf-8")
    return str(tmp_path)


def test_live_offer_ids_leave_uploaded_products_out(tmp_path):
    repo = _repo(tmp_path)
    arts = {c["article"] for c in pr.load_candidates(repo, live_offer_ids=["HM-1000000001-S", "MS-X-1"])}
    assert "1000000001" not in arts and "1000000002" in arts and len(arts) == 13


def test_todays_uploads_count_without_a_fresh_list(tmp_path):
    repo = _repo(tmp_path, new_today=["HM-1000000001-M"])
    assert "1000000001" not in {c["article"] for c in pr.load_candidates(repo)}


def test_urgent_no_longer_takes_the_whole_day(tmp_path):
    repo = _repo(tmp_path)
    rules = {"daily_offers": 1000, "min_sale_usd": 0, "max_cost_try": 0,
             "priorities": {"H&M/Underwear": "urgent"}}
    stale = pr.build_plan(rules, pr.load_candidates(repo, live_offer_ids=[]))
    fresh = pr.build_plan(rules, pr.load_candidates(repo, live_offer_ids=["HM-1000000001-S"]))
    assert stale["allocation"] == {"H&M/Underwear": 900, "H&M/Dress": 100}
    assert fresh["allocation"] == {"H&M/Underwear": 300, "H&M/Dress": 600}


def test_offer_list_failure_falls_back_to_cache(monkeypatch):
    import catalog_status

    def boom():
        raise RuntimeError("ozon down")
    monkeypatch.setattr(catalog_status, "live_offer_ids", boom)
    real = rnp.__dict__.get("_real_live_offer_ids_now")
    assert real is not None and real() is None
