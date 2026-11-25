"""catalog_status.main() live price file (catalog_live_prices.json). No network."""
import json

import pytest

import catalog_status


LIVE = ["MS-A-1", "MS-B-2", "MS-C-3", "MS-D-4", "HM-E-5"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # relative history files resolve to an empty dir
    calls = []

    def fake_call(endpoint, params=None):
        calls.append(endpoint)
        raise AssertionError(f"unexpected Ozon call: {endpoint}")

    def fake_stock(ids):
        calls.append("get_real_stock")
        return {"MS-A-1": 3, "HM-E-5": 1}

    monkeypatch.setattr(catalog_status, "call", fake_call)
    monkeypatch.setattr(catalog_status, "live_offer_ids", lambda: (calls.append("live_offer_ids"), list(LIVE))[1])
    monkeypatch.setattr(catalog_status, "get_real_stock", fake_stock)
    monkeypatch.setattr(catalog_status, "OUT", tmp_path / "catalog_status.json")
    monkeypatch.setattr(catalog_status, "LIVE_IDS_OUT", tmp_path / "catalog_live_offer_ids.json")
    monkeypatch.setattr(catalog_status, "LIVE_PRICES_OUT", tmp_path / "catalog_live_prices.json")
    return tmp_path, calls, monkeypatch


def test_prices_written_atomically_and_invalid_dropped(env):
    tmp_path, calls, mp = env
    mp.setattr(catalog_status, "fetch_final_prices",
               lambda ids: (calls.append("fetch_final_prices"),
                            {"MS-A-1": 25.5, "MS-B-2": None, "MS-C-3": 0.0, "MS-D-4": -3.0,
                             "HM-E-5": 70, "NOT-LIVE": 10.0})[1])
    replaced = []
    real_replace = catalog_status.os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    mp.setattr(catalog_status.os, "replace", spy)

    assert catalog_status.main() == 0
    out = tmp_path / "catalog_live_prices.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    status = json.loads((tmp_path / "catalog_status.json").read_text(encoding="utf-8"))
    assert data == {"built": status["built"], "count": 2, "prices": {"HM-E-5": 70.0, "MS-A-1": 25.5}}
    assert isinstance(data["prices"]["HM-E-5"], float)
    assert (str(out) + ".tmp", str(out)) in replaced
    assert not (tmp_path / "catalog_live_prices.json.tmp").exists()


def test_price_fetch_failure_keeps_other_files(env, capsys):
    tmp_path, calls, mp = env
    old = tmp_path / "catalog_live_prices.json"
    old.write_text('{"built": "old", "count": 0, "prices": {}}', encoding="utf-8")

    def boom(ids):
        raise RuntimeError("Ozon 500")

    mp.setattr(catalog_status, "fetch_final_prices", boom)
    assert catalog_status.main() == 0
    status = json.loads((tmp_path / "catalog_status.json").read_text(encoding="utf-8"))
    assert status["total"]["live"] == len(LIVE)
    assert status["total"]["for_sale"] == 2
    ids = json.loads((tmp_path / "catalog_live_offer_ids.json").read_text(encoding="utf-8"))
    assert ids["offer_ids"] == sorted(LIVE)
    assert old.read_text(encoding="utf-8") == '{"built": "old", "count": 0, "prices": {}}'
    assert "WARNING" in capsys.readouterr().out


def test_only_read_helpers_called(env):
    tmp_path, calls, mp = env
    mp.setattr(catalog_status, "fetch_final_prices",
               lambda ids: (calls.append("fetch_final_prices"), {})[1])
    assert catalog_status.main() == 0
    assert calls == ["live_offer_ids", "get_real_stock", "fetch_final_prices"]
    data = json.loads((tmp_path / "catalog_live_prices.json").read_text(encoding="utf-8"))
    assert data["count"] == 0 and data["prices"] == {}
