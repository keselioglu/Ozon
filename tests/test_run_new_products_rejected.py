"""Offers imported but refused at moderation are not counted and get replaced (real test 2)."""
import sys
import types

import ozon_client
import run_new_products as rnp
from ozon_mapping import TYPE_ID_PANTIES


def test_rejected_after_import_reads_card_errors(monkeypatch):
    items = [
        {"offer_id": "A", "statuses": {"is_created": False},
         "errors": [{"code": "SPU_ALREADY_EXISTS_IN_ANOTHER_ACCOUNT", "level": "ERROR_LEVEL_ERROR"}]},
        {"offer_id": "B", "statuses": {"is_created": True}, "errors": []},
        {"offer_id": "C", "statuses": {"is_created": False}, "errors": []},            # still moderating
        {"offer_id": "D", "statuses": {"is_created": True},
         "errors": [{"code": "X", "level": "ERROR_LEVEL_WARNING"}]},                   # warning only
    ]
    monkeypatch.setattr(ozon_client, "call", lambda path, body: {"items": items})
    assert rnp.rejected_after_import(["A", "B", "C", "D"]) == {"A": ["SPU_ALREADY_EXISTS_IN_ANOTHER_ACCOUNT"]}


def _entry(brand, art, n):
    pre = "MS" if brand == "M&S" else "HM"
    return (brand, "Underwear", [{"offer_id": f"{pre}-{art}-{i}", "type_id": TYPE_ID_PANTIES, "price": "1",
                                  "name": "x", "_stock": 3} for i in range(n)], art)


def test_run_replaces_rejected_with_spares(monkeypatch):
    chosen = [_entry("H&M", "1000000001", 2), _entry("H&M", "1000000002", 2)]
    spares = [_entry("H&M", "1000000003", 2)]
    remaining = {"total": 4, "plan_total": 4, "created_before": 0, "quota_left": 100, "per_category": {}}
    monkeypatch.setattr(rnp, "targets_for_today", lambda t, n, o=None: (remaining, {("H&M", "Underwear"): 4}, {"H&M"}))
    monkeypatch.setattr(rnp, "labels_for", lambda b, w, o: [])
    monkeypatch.setattr(rnp, "ms_candidates", lambda *a: {})

    def pick(*a):
        a[-1]["reserve"] = {"planned": 1, "built": 2, "used_for_shortfall": 0, "used_for_failed_imports": 0}
        return list(chosen), list(spares)
    monkeypatch.setattr(rnp, "pick_and_build", pick)
    monkeypatch.setattr(rnp, "save_ms_rows", lambda p: None)
    monkeypatch.setattr(rnp, "log_new_items", lambda its, t: None)
    monkeypatch.setattr(rnp, "write_json", lambda p, d: None)
    sent = []
    monkeypatch.setattr(rnp, "upload", lambda its: sent.append([i["offer_id"] for i in its]) or ["t"])
    monkeypatch.setattr(rnp, "wait_for_import", lambda tids: ({o: "imported" for o in sent[-1]}, {}))
    pushed = []
    monkeypatch.setitem(sys.modules, "update_stocks",
                        types.SimpleNamespace(push_stock_updates_for=lambda u: pushed.append(len(u))))
    monkeypatch.setattr(rnp, "rejected_after_import",
                        lambda ids: {o: ["SPU_ALREADY_EXISTS_IN_ANOTHER_ACCOUNT"] for o in ids if "1000000002" in o})
    monkeypatch.setattr(rnp, "wait_for_prices", lambda ids: set(ids))
    rep = rnp.run(today="2026-11-27")
    assert sent[1] == ["HM-1000000003-0", "HM-1000000003-1"]      # spare product uploaded instead
    assert rep["created"] == {"H&M/Underwear": 4}                  # 2 ok + 2 replacement, rejected not counted
    assert set(rep["rejected"]) == {"HM-1000000002-0", "HM-1000000002-1"}
    assert rep["stock_pushed"] == 6 and pushed == [4, 2]
    assert rep["reserve"]["used_for_failed_imports"] == 2
    assert rep["state"] == "completed_with_errors"
