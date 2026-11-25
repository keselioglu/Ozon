"""Offline tests for new_product_plan.py -- no network, no real data files."""
import json
import os

import pytest

import daily_plan
import new_product_plan as npp

LABELS = list(daily_plan.CATEGORY_LABELS.values())


def _inv(cats=None, hm=None):
    return {"brands": {"M&S": {"categories": cats or {}}, "H&M": hm or {}}}


# --- load / save / validate ---------------------------------------------------

def test_missing_file_gives_empty_plan(tmp_path):
    assert npp.load_plan(str(tmp_path / "nope.json")) == npp.default_plan()


@pytest.mark.parametrize("text", ["{not json", "[]", '{"categories": 5}', ""])
def test_corrupt_file_gives_empty_plan(tmp_path, text):
    p = tmp_path / "new_product_plan.json"
    p.write_text(text, encoding="utf-8")
    assert npp.load_plan(str(p)) == npp.default_plan()


def test_load_drops_invalid_entries_keeps_valid(tmp_path):
    p = tmp_path / "new_product_plan.json"
    p.write_text(json.dumps({"version": 1, "categories": {
        "M&S/Pajama": {"priority": "high", "urls_to_add": 40},
        "M&S/Socks": {"priority": "urgent", "urls_to_add": 1},
        "M&S/Dress": {"priority": "low", "urls_to_add": -2},
        "H&M/All": {"priority": "low", "urls_to_add": 1},
        "nokey": {"priority": "low"},
    }}), encoding="utf-8")
    plan = npp.load_plan(str(p))
    assert plan["categories"] == {"M&S/Pajama": {"priority": "high", "urls_to_add": 40}}


def test_save_load_round_trip(tmp_path):
    path = str(tmp_path / "new_product_plan.json")
    plan = npp.set_entry(npp.default_plan(), "M&S", "Pajama", priority="high", urls_to_add=40)
    written = npp.save_plan(plan, path)
    assert written["updated_at"]
    loaded = npp.load_plan(path)
    assert loaded == written
    assert npp.get_entry(loaded, "M&S", "Pajama") == {"priority": "high", "urls_to_add": 40}
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


@pytest.mark.parametrize("entry", [
    {"priority": "urgent", "urls_to_add": 1},
    {"priority": "high", "urls_to_add": -1},
    {"priority": "high", "urls_to_add": True},
    {"priority": "high", "urls_to_add": "5"},
    {"priority": "high", "urls_to_add": 2.5},
])
def test_invalid_values_rejected_and_nothing_written(tmp_path, entry):
    path = tmp_path / "new_product_plan.json"
    plan = {"version": 1, "categories": {"M&S/Pajama": entry}}
    assert npp.validate_plan(plan)
    with pytest.raises(ValueError):
        npp.save_plan(plan, str(path))
    assert not path.exists()
    assert os.listdir(tmp_path) == []


def test_invalid_save_keeps_existing_file(tmp_path):
    path = str(tmp_path / "new_product_plan.json")
    good = npp.save_plan(npp.set_entry(npp.default_plan(), "M&S", "Socks", urls_to_add=3), path)
    with pytest.raises(ValueError):
        npp.save_plan({"categories": {"M&S/Socks": {"urls_to_add": -1}}}, path)
    assert npp.load_plan(path) == good


def test_get_entry_defaults():
    assert npp.get_entry(npp.default_plan(), "M&S", "Pajama") == {"priority": "medium", "urls_to_add": 0}
    assert npp.get_entry(None, "M&S", "Pajama") == {"priority": "medium", "urls_to_add": 0}


# --- set_entry ------------------------------------------------------------------

def test_set_entry_returns_new_plan():
    base = npp.default_plan()
    new = npp.set_entry(base, "M&S", "Pajama", priority="low")
    assert base["categories"] == {}
    assert new["categories"]["M&S/Pajama"] == {"priority": "low", "urls_to_add": 0}
    newer = npp.set_entry(new, "M&S", "Pajama", urls_to_add=9)
    assert newer["categories"]["M&S/Pajama"] == {"priority": "low", "urls_to_add": 9}


@pytest.mark.parametrize("kwargs", [
    {"priority": "urgent"}, {"urls_to_add": -1}, {"urls_to_add": True}, {"urls_to_add": "3"},
])
def test_set_entry_rejects_invalid(kwargs):
    with pytest.raises(ValueError):
        npp.set_entry(npp.default_plan(), "M&S", "Pajama", **kwargs)


def test_set_entry_refuses_hm():
    with pytest.raises(ValueError):
        npp.set_entry(npp.default_plan(), "H&M", "All", urls_to_add=1)


# --- add all / add half ---------------------------------------------------------

def test_add_all_and_half():
    assert npp.add_all({"eligible_urls": 7}) == 7
    assert npp.add_half({"eligible_urls": 7}) == 3
    assert npp.add_all({"eligible_urls": 0}) == 0
    assert npp.add_half({"eligible_urls": 0}) == 0
    assert npp.add_all({"eligible_urls": None}) == 0


# --- rows -------------------------------------------------------------------------

def test_empty_inventory_gives_zeros_in_label_order():
    rows = npp.build_rows({}, npp.default_plan())
    ms = [r for r in rows if r["brand"] == "M&S"]
    assert [r["category"] for r in ms] == LABELS
    for r in ms:
        assert r["total_urls"] == 0 and r["eligible_urls"] == 0
        assert r["priority"] == "medium" and r["urls_to_add"] == 0
        assert r["editable"] is True and r["over_eligible"] is False
    assert npp.build_rows(None, None)[0]["total_urls"] == 0


def test_counts_from_inventory_keys():
    inv = _inv({"Pajama": {"live_products": 10, "crawled_not_live_products": 4,
                           "discovered_not_live_products": 6},
                "Socks": {"crawled_not_live_products": 2}})
    rows = {r["category"]: r for r in npp.build_rows(inv, npp.default_plan()) if r["brand"] == "M&S"}
    assert rows["Pajama"]["total_urls"] == 20
    assert rows["Pajama"]["eligible_urls"] == 10
    assert rows["Socks"]["total_urls"] == 2
    assert rows["Socks"]["eligible_urls"] == 2


def test_rows_sorted_by_priority_then_label_order():
    plan = npp.default_plan()
    plan = npp.set_entry(plan, "M&S", "Top", priority="very_high")
    plan = npp.set_entry(plan, "M&S", "Underwear", priority="very_low")
    plan = npp.set_entry(plan, "M&S", "Dress", priority="high")
    plan = npp.set_entry(plan, "M&S", "Socks", priority="high")
    plan = npp.set_entry(plan, "M&S", "Pajama", priority="low")
    rows = [r for r in npp.build_rows({}, plan) if r["brand"] == "M&S"]
    ranks = [npp.PRIORITY_RANK[r["priority"]] for r in rows]
    assert ranks == sorted(ranks)
    cats = [r["category"] for r in rows]
    assert cats[0] == "Top"
    assert cats[1:3] == ["Socks", "Dress"]  # tie -> CATEGORY_LABELS order
    assert cats[-2:] == ["Pajama", "Underwear"]
    medium = [c for c in cats if c not in ("Top", "Dress", "Socks", "Pajama", "Underwear")]
    assert medium == [l for l in LABELS if l in medium]


def test_hm_row_read_only():
    rows = npp.build_rows(_inv(hm={"live": 50, "for_sale": 30}), npp.default_plan())
    hm = [r for r in rows if r["brand"] == "H&M"]
    assert len(hm) == 1
    hm = hm[0]
    assert rows[-1] is hm
    assert hm["editable"] is False
    assert hm["total_urls"] is None and hm["eligible_urls"] is None
    assert hm["note"] == "no add pipeline yet / website check pending"
    assert hm["live"] == 50


def test_over_eligible_flag():
    inv = _inv({"Pajama": {"crawled_not_live_products": 3}})
    plan = npp.set_entry(npp.default_plan(), "M&S", "Pajama", urls_to_add=5)
    plan = npp.set_entry(plan, "M&S", "Socks", urls_to_add=0)
    rows = {r["category"]: r for r in npp.build_rows(inv, plan) if r["brand"] == "M&S"}
    assert rows["Pajama"]["over_eligible"] is True
    assert rows["Socks"]["over_eligible"] is False
    plan = npp.set_entry(plan, "M&S", "Pajama", urls_to_add=3)
    rows = {r["category"]: r for r in npp.build_rows(inv, plan) if r["brand"] == "M&S"}
    assert rows["Pajama"]["over_eligible"] is False
