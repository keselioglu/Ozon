import json

import catalog_status


def _no_network(*a, **k):
    raise AssertionError("network/live_offer_ids must not be called")


def test_write_live_ids_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_status, "live_offer_ids", _no_network)
    monkeypatch.setattr(catalog_status, "call", _no_network)
    out = tmp_path / "catalog_live_offer_ids.json"
    ids = ["MS-T123-BLK-M", "HM-1-2-3", "MS-T001-WHT-38"]
    built = "2026-11-24T04:00:00"

    catalog_status.write_live_ids(out, ids, built)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"built": built, "count": 3, "offer_ids": sorted(ids)}
    assert [p.name for p in tmp_path.iterdir()] == [out.name]


def test_write_live_ids_overwrites_existing(tmp_path):
    out = tmp_path / "catalog_live_offer_ids.json"
    out.write_text("old, not json", encoding="utf-8")
    catalog_status.write_live_ids(out, [], "2026-11-24T05:00:00")
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "built": "2026-11-24T05:00:00", "count": 0, "offer_ids": []}
    assert not (tmp_path / "catalog_live_offer_ids.json.tmp").exists()
