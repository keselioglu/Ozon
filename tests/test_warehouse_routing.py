"""Tests for warehouse_routing.py's $60 price-only rule (task #47).
No live Ozon calls: fetch_final_prices / fetch_weights / call are stubbed and
the state file is redirected to tmp_path."""
import json

import pytest

import warehouse_routing as wr

SMALL = wr.SMALL_WAREHOUSE_ID
REGULAR = wr.REGULAR_WAREHOUSE_ID


@pytest.fixture(autouse=True)
def no_live_calls(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("live Ozon call attempted")
    monkeypatch.setattr(wr, "call", boom)
    monkeypatch.setattr(wr, "fetch_weights", boom)
    monkeypatch.setattr(wr, "WAREHOUSE_STATE_FILE", str(tmp_path / "warehouse_assignments.json"))


def stub_prices(monkeypatch, prices):
    monkeypatch.setattr(wr, "fetch_final_prices", lambda ids: {o: prices.get(o) for o in ids})


@pytest.mark.parametrize("price,expected", [
    (59.99, SMALL), (60.0, REGULAR), (120, REGULAR), (1, SMALL),
    (None, None), (0, None), (-5, None), ("abc", None),
])
def test_decide_target_warehouse(price, expected):
    assert wr.decide_target_warehouse(price) == expected


def entries_for(updates, oid):
    return {u["warehouse_id"]: u["stock"] for u in updates if u["offer_id"] == oid}


def test_build_routed_stock_updates(monkeypatch):
    stub_prices(monkeypatch, {"MS-A": 59.99, "MS-B": 60.0, "MS-C": 120.0, "HM-D": 30.0, "HM-E": 75.0})
    stock = {"MS-A": 3, "MS-B": 4, "MS-C": 5, "MS-X": 7, "HM-D": 2, "HM-E": 6}
    updates, skipped = wr.build_routed_stock_updates(stock)

    assert entries_for(updates, "MS-A") == {SMALL: 3, REGULAR: 0}
    assert entries_for(updates, "MS-B") == {REGULAR: 4, SMALL: 0}
    assert entries_for(updates, "MS-C") == {REGULAR: 5, SMALL: 0}
    assert entries_for(updates, "HM-D") == {SMALL: 2, REGULAR: 0}
    assert entries_for(updates, "HM-E") == {REGULAR: 6, SMALL: 0}
    for oid in ("MS-A", "MS-B", "MS-C", "HM-D", "HM-E"):
        assert len([u for u in updates if u["offer_id"] == oid]) == 2
    assert skipped == ["MS-X"]
    assert entries_for(updates, "MS-X") == {}


def test_save_warehouse_assignments_merges(monkeypatch):
    with open(wr.WAREHOUSE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"MS-OLD": REGULAR, "MS-A": REGULAR}, f)
    stub_prices(monkeypatch, {"MS-A": 10.0})
    wr.build_routed_stock_updates({"MS-A": 1})
    state = wr.load_warehouse_assignments()
    assert state["MS-OLD"] == REGULAR
    assert state["MS-A"] == SMALL
