"""run_new_products.pick_and_build: the 15% reserve (owner instruction 2026-11-26)."""
import pytest

import daily_plan
import hm_catalog
import run_new_products as rnp
import upload_to_ozon


def test_reserve_for():
    assert daily_plan.reserve_for(100) == 15
    assert daily_plan.reserve_for(10) == 2      # ceil
    assert daily_plan.reserve_for(0) == 0
    assert daily_plan.reserve_for(None) == 0


def _product(article, n_sizes):
    return {"rec": {"article": article, "title": "t"}, "detail": {"sizes": n_sizes}}


@pytest.fixture
def fake_build(monkeypatch):
    monkeypatch.setattr(rnp, "translate", lambda *a, **k: ({"name": "x"}, None))
    monkeypatch.setattr(hm_catalog, "specs_for_translation", lambda det: "{}")
    monkeypatch.setattr(hm_catalog, "build_items",
                        lambda rec, det, tr, rate, dims: [{"offer_id": f"HM-{rec['article']}-{i}", "price": "1",
                                                           "name": "x", "_stock": 5} for i in range(det["sizes"])])
    monkeypatch.setattr(upload_to_ozon, "find_existing_offer_ids", lambda ids: set())
    monkeypatch.setattr(upload_to_ozon, "package_dimensions", lambda title: {})


def _remaining(total):
    return {"total": total, "plan_total": total, "per_category": {}, "fill_remainder": False}


def test_spares_built_past_allocation_and_kept_separate(fake_build):
    hm_found = {"Dress": [_product(str(i), 4) for i in range(10)]}
    report = {}
    chosen, spares = rnp.pick_and_build({}, hm_found, {("H&M", "Dress"): 20}, set(), _remaining(20), False, report)
    assert sum(len(e[2]) for e in chosen) == 20          # target, never more
    assert sum(len(e[2]) for e in spares) == 4           # reserve ceil(20*15%)=3 -> one whole product
    assert report["reserve"] == {"planned": 3, "built": 4, "used_for_shortfall": 0, "used_for_failed_imports": 0}


def test_spares_complete_a_short_category(fake_build):
    # Dress 10 + Top 10 = 20 planned; Top has only one 4-size product -> 6 short.
    hm_found = {"Dress": [_product(f"d{i}", 2) for i in range(10)], "Top": [_product("t0", 4)]}
    want = {("H&M", "Dress"): 10, ("H&M", "Top"): 10}
    report = {}
    chosen, spares = rnp.pick_and_build({}, hm_found, want, set(), _remaining(20), False, report)
    total = sum(len(e[2]) for e in chosen)
    assert total == 18  # 10 Dress + 4 Top + 2 spare Dress products (reserve 3 -> 4 offers built, all used)
    assert report["reserve"]["used_for_shortfall"] == 4
    assert total <= 20


def test_take_spares_whole_products_only():
    spares = [("H&M", "Dress", [1, 2, 3], "a"), ("H&M", "Dress", [1], "b"), ("H&M", "Top", [1, 2], "c")]
    got = rnp.take_spares(spares, 3)
    assert [e[3] for e in got] == ["a"]
    assert [e[3] for e in spares] == ["b", "c"]
