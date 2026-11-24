"""Offline tests for daily_plan.py -- no network, no real data files."""
import csv
import importlib
import json
from collections import Counter

import pytest

import daily_plan as dp


def _plan(total=5, allocations=None, fill=True, overrides=None):
    return {
        "version": 1,
        "default": {"total": total, "allocations": allocations or {}, "fill_remainder": fill},
        "overrides": overrides or {},
        "updated_at": None,
    }


# --- load / save / validate ---------------------------------------------------

def test_missing_file_gives_default(tmp_path):
    plan = dp.load_plan(str(tmp_path / "nope.json"))
    day = dp.plan_for_date(plan, "2026-11-25")
    assert day["total"] == 500
    assert day["allocations"] == {}
    assert day["fill_remainder"] is True


def test_corrupt_file_falls_back_without_raising(tmp_path):
    p = tmp_path / "daily_plan.json"
    p.write_text("{not json", encoding="utf-8")
    assert dp.load_plan(str(p)) == dp.default_plan()


def test_invalid_saved_plan_falls_back(tmp_path):
    p = tmp_path / "daily_plan.json"
    p.write_text(json.dumps(_plan(total=-1)), encoding="utf-8")
    assert dp.load_plan(str(p)) == dp.default_plan()


def test_save_then_load_roundtrip(tmp_path):
    p = str(tmp_path / "daily_plan.json")
    plan = _plan(total=500, allocations={"M&S": {"Pajama": 120, "Socks": 80}},
                 overrides={"2026-11-26": {"total": 300, "allocations": {"M&S": {"Dress": 50}},
                                           "fill_remainder": False}})
    saved = dp.save_plan(plan, p)
    loaded = dp.load_plan(p)
    assert loaded == saved
    assert loaded["updated_at"]
    assert {k: v for k, v in loaded.items() if k != "updated_at"} == \
        {k: v for k, v in plan.items() if k != "updated_at"}
    assert list(tmp_path.iterdir()) == [tmp_path / "daily_plan.json"]  # no temp file left


def test_save_rejects_invalid(tmp_path):
    p = tmp_path / "daily_plan.json"
    with pytest.raises(ValueError):
        dp.save_plan(_plan(total=1, allocations={"M&S": {"Pajama": 2}}), str(p))
    assert not p.exists()


def test_validate_ok():
    assert dp.validate_plan(_plan(total=3, allocations={"M&S": {"Pajama": 2, "Socks": 1}})) == []
    assert dp.validate_plan(dp.default_plan()) == []


def test_validate_rejects_sum_over_total():
    assert dp.validate_plan(_plan(total=2, allocations={"M&S": {"Pajama": 2, "Socks": 1}}))


def test_validate_rejects_negative_and_non_int():
    assert dp.validate_plan(_plan(allocations={"M&S": {"Pajama": -1}}))
    assert dp.validate_plan(_plan(allocations={"M&S": {"Pajama": 1.5}}))
    assert dp.validate_plan(_plan(allocations={"M&S": {"Pajama": "2"}}))
    assert dp.validate_plan(_plan(allocations={"M&S": {"Pajama": True}}))
    assert dp.validate_plan(_plan(total=2001))
    assert dp.validate_plan(_plan(total=-1))
    assert dp.validate_plan(_plan(total=10.0))


def test_validate_checks_overrides():
    bad = _plan(overrides={"2026-11-26": {"total": 1, "allocations": {"M&S": {"Socks": 5}}}})
    assert dp.validate_plan(bad)
    assert dp.validate_plan(_plan(overrides={"tomorrow": {"total": 1}}))


def test_override_wins_over_default():
    plan = _plan(total=500, overrides={"2026-11-26": {"total": 100, "allocations": {},
                                                      "fill_remainder": False}})
    assert dp.plan_for_date(plan, "2026-11-26")["total"] == 100
    assert dp.plan_for_date(plan, "2026-11-26")["fill_remainder"] is False
    assert dp.plan_for_date(plan, "2026-11-25")["total"] == 500


# --- remaining_today ----------------------------------------------------------

@pytest.mark.parametrize("quota,created", [(0, 0), (10, 0), (2000, 0), (2000, 499),
                                           (2000, 600), (-5, 0), (3, 1)])
def test_remaining_bounds(quota, created):
    plan = _plan(total=500, allocations={"M&S": {"Pajama": 10}})
    r = dp.remaining_today(plan, quota, created, Counter({("M&S", "Pajama"): 25}), "2026-11-25")
    assert 0 <= r["total"] <= max(0, quota)
    assert r["total"] == max(0, min(quota, 500 - created))
    assert r["per_category"][("M&S", "Pajama")] == 0


def test_remaining_none_quota_still_applies_plan():
    r = dp.remaining_today(_plan(total=500), None, 120, Counter(), "2026-11-25")
    assert r["total"] == 380


def test_remaining_per_category():
    plan = _plan(total=500, allocations={"M&S": {"Pajama": 120, "Socks": 80}}, fill=False)
    r = dp.remaining_today(plan, 2000, 30, Counter({("M&S", "Pajama"): 20}), "2026-11-25")
    assert r == {"total": 470, "per_category": {("M&S", "Pajama"): 100, ("M&S", "Socks"): 80},
                 "fill_remainder": False}


# --- apply_cap ----------------------------------------------------------------

ITEMS = [("P1", "Pajama"), ("S1", "Socks"), ("T1", "Top"), ("P2", "Pajama"), ("S2", "Socks"),
         ("P3", "Pajama"), ("T2", "Top"), ("P4", "Pajama"), ("S3", "Socks")]


def _key(item):
    return ("M&S", item[1])


def _remaining(fill):
    return {"total": 5, "per_category": {("M&S", "Pajama"): 2, ("M&S", "Socks"): 1},
            "fill_remainder": fill}


def test_apply_cap_fill_remainder():
    accepted, deferred = dp.apply_cap(ITEMS, _remaining(True), _key)
    # pass 1: P1, S1, P2; pass 2 in list order: T1, S2
    assert [i[0] for i in accepted] == ["P1", "S1", "T1", "P2", "S2"]
    assert [i[0] for i in deferred] == ["P3", "T2", "P4", "S3"]
    assert Counter(i[1] for i in accepted) == Counter({"Pajama": 2, "Socks": 2, "Top": 1})


def test_apply_cap_no_fill_remainder():
    accepted, deferred = dp.apply_cap(ITEMS, _remaining(False), _key)
    assert [i[0] for i in accepted] == ["P1", "S1", "P2"]
    assert [i[0] for i in deferred] == ["T1", "S2", "P3", "T2", "P4", "S3"]


def test_apply_cap_total_limits_allocations():
    r = {"total": 1, "per_category": {("M&S", "Pajama"): 2}, "fill_remainder": True}
    accepted, _ = dp.apply_cap(ITEMS, r, _key)
    assert [i[0] for i in accepted] == ["P1"]


def test_apply_cap_unmapped_only_in_remainder():
    items = [("U1", None), ("P1", "Pajama"), ("U2", None)]
    r = {"total": 2, "per_category": {("M&S", None): 5, ("M&S", "Pajama"): 1},
         "fill_remainder": False}
    accepted, _ = dp.apply_cap(items, r, _key)
    assert [i[0] for i in accepted] == ["P1"]
    r["fill_remainder"] = True
    accepted, deferred = dp.apply_cap(items, r, _key)
    assert [i[0] for i in accepted] == ["U1", "P1"]
    assert [i[0] for i in deferred] == ["U2"]


def test_apply_cap_zero_total():
    r = {"total": 0, "per_category": {}, "fill_remainder": True}
    accepted, deferred = dp.apply_cap(ITEMS, r, _key)
    assert accepted == [] and deferred == ITEMS


# --- labels / brand / created_today ------------------------------------------

def test_labels_and_brand():
    from ozon_mapping import TYPE_ID_PAJAMA, TYPE_ID_SOCKS
    assert dp.CATEGORY_LABELS[TYPE_ID_PAJAMA] == "Pajama"
    assert dp.category_label_for_type(TYPE_ID_SOCKS) == "Socks"
    assert dp.category_label_for_type(123) is None
    assert dp.category_label_for_name("Saten Pijama Takımı") == "Pajama"
    assert dp.category_label_for_name("Bilinmeyen Ürün") is None
    assert dp.brand_for_offer_id("MS-T81006840V-PEBBLE-36") == "M&S"
    assert dp.brand_for_offer_id("SMLMS-T81006840V-PEBBLE-36") == "M&S"
    assert dp.brand_for_offer_id("HM-12345-S") == "H&M"
    assert dp.brand_for_offer_id("2-HM-12345-S") == "H&M"
    assert dp.brand_for_offer_id("XYZ-1") == "Other"


def test_created_today(tmp_path):
    products = tmp_path / "products.csv"
    with open(products, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "ms_article_code", "name"])
        w.writerow(["u1", "T81006840V", "Saten Pijama Takımı"])
        w.writerow(["u2", "T12345678", "Pamuklu Çorap"])
        w.writerow(["u3", "T99999999", "Bilinmeyen Ürün"])
    log = tmp_path / "new_items_submitted.json"
    log.write_text(json.dumps({"date": "2026-11-25", "new_offer_ids": [
        "MS-T81006840V-PEBBLE-36", "MS-T81006840V-PEBBLE-38", "MS-T12345678-BLACK-M",
        "MS-T99999999-RED-S", "MS-T00000001-RED-S", "MS-10000000601019-S"]}), encoding="utf-8")
    total, by_cat = dp.created_today(str(log), "2026-11-25", str(products))
    assert total == 6
    assert by_cat == Counter({("M&S", "Pajama"): 2, ("M&S", "Socks"): 1})
    # a different day's log counts as nothing created today
    assert dp.created_today(str(log), "2026-11-26", str(products)) == (0, Counter())
    # missing products.csv: everything counts toward the total only
    assert dp.created_today(str(log), "2026-11-25", str(tmp_path / "x.csv")) == (6, Counter())
    assert dp.created_today(str(tmp_path / "missing.json"), "2026-11-25") == (0, Counter())


def test_import_makes_no_ozon_call(monkeypatch):
    import ozon_client
    import requests

    def boom(*a, **k):
        raise AssertionError("network call at import")

    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(ozon_client, "call", boom)
    importlib.reload(dp)
