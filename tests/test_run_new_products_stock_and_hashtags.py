"""Real 500-offer run 2026-11-27: stock pushed before Ozon had prices (none got stock), and three
cards refused only for their hashtags."""
import sys
import types

import pytest

import ozon_mapping
import run_new_products as rnp
import warehouse_routing
from ozon_mapping import TYPE_ID_PANTIES
from upload_to_ozon import ATTR_HASHTAGS


def test_clean_hashtags_drops_broken_and_brand_tags():
    s = "#трусы #трусысприн�ом #марксэндспенсер #Трусы #детскоебелье #bad"
    assert ozon_mapping.clean_hashtags(s) == "#трусы #детскоебелье"
    assert ozon_mapping.hashtag_attribute(ATTR_HASHTAGS, "#�") == []
    assert ozon_mapping.hashtag_attribute(ATTR_HASHTAGS, "#юбка_миди") == [
        {"id": ATTR_HASHTAGS, "values": [{"value": "#юбка_миди"}]}]


def test_validate_hashtags_rejects_broken_character():
    at = pytest.importorskip("auto_translate")
    with pytest.raises(ValueError):
        at._validate_hashtags("#a1 #трусы #б #в #трусысприн�ом".replace("#a1", "#г"))
    at._validate_hashtags("#трусы #женскоебелье #кружево #хлопок #бельё")


def test_wait_for_prices_polls_until_ready(monkeypatch):
    calls = []

    def prices(ids):
        calls.append(1)
        return {o: (25.0 if len(calls) > 1 else 0.0) for o in ids}
    monkeypatch.setattr(warehouse_routing, "fetch_final_prices", prices)
    monkeypatch.setattr(rnp.time, "sleep", lambda s: None)
    assert rnp.wait_for_prices(["A", "B"], timeout=60, every=1) == {"A", "B"} and len(calls) == 2


def test_wait_for_prices_gives_up_at_timeout(monkeypatch):
    monkeypatch.setattr(warehouse_routing, "fetch_final_prices", lambda ids: {"A": 30.0})
    assert rnp.wait_for_prices(["A", "B"], timeout=0, every=1) == {"A"}


def test_retry_failed_images_resends_only_flagged_cards(monkeypatch):
    import ozon_client
    monkeypatch.setattr(ozon_client, "call", lambda path, body: {"items": [
        {"offer_id": "A", "id": 11, "errors": [{"code": "pics_http_error", "level": "ERROR_LEVEL_WARNING"}]},
        {"offer_id": "B", "id": 12, "errors": [{"code": "double_without_merger_offer"}]},
        {"offer_id": "C", "id": 13, "errors": []}]})
    sent = []
    monkeypatch.setattr(rnp, "upload", lambda its: sent.append(its) or ["t"])
    monkeypatch.setattr(rnp, "wait_for_import", lambda t: ({i["offer_id"]: "imported" for i in sent[-1]}, {}))
    items = [{"offer_id": o, "images": ["u1", "u2"], "primary_image": "u2"} for o in "ABC"]
    assert rnp._real_retry_failed_images(items) == ["A"]
    assert sent == [[items[0]]]                     # the same card, unchanged


def _entry(art, n):
    return ("H&M", "Underwear", [{"offer_id": f"HM-{art}-{i}", "type_id": TYPE_ID_PANTIES, "price": "1",
                                  "name": "x", "_stock": 3,
                                  "attributes": [{"id": 1, "values": []}, {"id": ATTR_HASHTAGS, "values": []}]}
                                 for i in range(n)], art)


@pytest.fixture
def stubbed(monkeypatch):
    remaining = {"total": 4, "plan_total": 4, "created_before": 0, "quota_left": 100, "per_category": {}}
    monkeypatch.setattr(rnp, "targets_for_today", lambda t, n, o=None: (remaining, {("H&M", "Underwear"): 4}, {"H&M"}))
    monkeypatch.setattr(rnp, "labels_for", lambda b, w, o: [])
    monkeypatch.setattr(rnp, "ms_candidates", lambda *a: {})

    def pick(*a):
        a[-1]["reserve"] = {"planned": 0, "built": 0, "used_for_shortfall": 0, "used_for_failed_imports": 0}
        return [_entry("1000000001", 2), _entry("1000000002", 2)], []
    monkeypatch.setattr(rnp, "pick_and_build", pick)
    monkeypatch.setattr(rnp, "save_ms_rows", lambda p: None)
    monkeypatch.setattr(rnp, "log_new_items", lambda its, t: None)
    sent = []
    monkeypatch.setattr(rnp, "upload", lambda its: sent.append(its) or ["t"])
    monkeypatch.setattr(rnp, "wait_for_import", lambda tids: ({i["offer_id"]: "imported" for i in sent[-1]}, {}))
    pushed = []
    monkeypatch.setitem(sys.modules, "update_stocks", types.SimpleNamespace(
        push_stock_updates_for=lambda u: pushed.append([x["offer_id"] for x in u]) or (len(u) * 2, 0)))
    return sent, pushed


def test_stock_only_for_priced_offers_and_reported(monkeypatch, stubbed):
    sent, pushed = stubbed
    monkeypatch.setattr(rnp, "wait_for_prices", lambda ids: {o for o in ids if "1000000001" in o})
    monkeypatch.setattr(rnp, "rejected_after_import", lambda ids: {})
    rep = rnp.run(today="2026-11-27")
    assert pushed == [["HM-1000000001-0", "HM-1000000001-1"]]
    assert rep["stock_pushed"] == 2 and sorted(rep["stock_not_pushed"]) == ["HM-1000000002-0", "HM-1000000002-1"]
    assert rep["state"] == "completed_with_errors"


def test_hashtag_only_rejection_is_resent_without_hashtags(monkeypatch, stubbed):
    sent, pushed = stubbed
    monkeypatch.setattr(rnp, "wait_for_prices", lambda ids: set(ids))
    monkeypatch.setattr(rnp, "rejected_after_import",
                        lambda ids: {o: ["BR_hashtag_brand"] for o in ids if "1000000002" in o})
    rep = rnp.run(today="2026-11-27")
    assert [i["offer_id"] for i in sent[1]] == ["HM-1000000002-0", "HM-1000000002-1"]
    assert all(a["id"] != ATTR_HASHTAGS for i in sent[1] for a in i["attributes"])
    assert rep["created"] == {"H&M/Underwear": 4} and not rep["rejected"]
    assert rep["hashtags_removed"] == ["HM-1000000002-0", "HM-1000000002-1"]
    assert rep["state"] == "completed"
