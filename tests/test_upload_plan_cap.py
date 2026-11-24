"""Offline tests for the daily-plan cap in upload_to_ozon.main() -- every Ozon
call is patched; files live in a tmp dir (monkeypatch.chdir)."""
import csv
import json

import pytest

import daily_plan as dp
import upload_to_ozon as up
from ozon_mapping import TYPE_ID_PAJAMA, TYPE_ID_SOCKS, TYPE_ID_TOP

TODAY = dp.utc_today()
NAMES = {TYPE_ID_PAJAMA: "Saten Pijama Takımı", TYPE_ID_SOCKS: "Pamuklu Çorap",
         TYPE_ID_TOP: "Bilinmeyen Ürün"}


def _offer_id(i):
    return f"MS-T{10000000 + i}-BLACK-M"


def _write_products(rows):
    """rows: list of type_ids, one product row each, in priority order."""
    with open("products.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ms_article_code", "name", "variant_sku", "color", "type_id"])
        for i, type_id in enumerate(rows):
            w.writerow([f"T{10000000 + i}", NAMES[type_id], _offer_id(i), "BLACK", type_id])


def _write_plan(total=500, allocations=None, fill=True):
    dp.save_plan({"version": 1, "default": {"total": total, "allocations": allocations or {},
                                            "fill_remainder": fill},
                  "overrides": {}, "updated_at": None})


class FakeOzon:
    def __init__(self, existing=(), quota_remaining=2000):
        self.existing = set(existing)
        self.quota_remaining = quota_remaining
        self.imports = []  # list of batches POSTed to /v3/product/import

    def call(self, path, payload=None, retries=3):
        if path == "/v3/product/import":
            self.imports.append(payload["items"])
            return {"result": {"task_id": len(self.imports)}}
        raise AssertionError(f"unexpected Ozon call {path}")

    def check_quota(self):
        if self.quota_remaining is None:
            return None
        return {"daily_create": {"limit": 2000, "usage": 2000 - self.quota_remaining, "reset_at": "x"}}

    @property
    def submitted(self):
        return [it["offer_id"] for batch in self.imports for it in batch]


@pytest.fixture
def ozon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = FakeOzon()

    def build(row, offer_id_suffix=""):
        return {"offer_id": row["variant_sku"], "type_id": int(row["type_id"]),
                "description_category_id": 200000933}, []

    monkeypatch.setattr(up, "call", fake.call)
    monkeypatch.setattr(up, "check_quota", fake.check_quota)
    monkeypatch.setattr(up, "build_ozon_item", build)
    monkeypatch.setattr(up, "fetch_live_article_codes_by_prefix", lambda: {})
    monkeypatch.setattr(up, "fetch_live_offer_ids_by_article", lambda: {})
    monkeypatch.setattr(up, "find_existing_offer_ids", lambda ids: set(ids) & fake.existing)
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    return fake


def _deferred_ids():
    with open(up.DEFERRED_LOG, encoding="utf-8") as f:
        return [it["offer_id"] for it in json.load(f)]


def _logged_ids():
    with open(up.NEW_ITEMS_LOG, encoding="utf-8") as f:
        log = json.load(f)
    assert log["date"] == TODAY
    return log["new_offer_ids"]


def test_caps_900_new_at_plan_500(ozon):
    _write_products([TYPE_ID_TOP] * 900)
    _write_plan(500)
    up.main([])
    assert len(ozon.submitted) == 500
    assert ozon.submitted == [_offer_id(i) for i in range(500)]
    assert _deferred_ids() == [_offer_id(i) for i in range(500, 900)]
    assert _logged_ids() == ozon.submitted


def test_already_created_today_counts_against_plan(ozon):
    _write_products([TYPE_ID_TOP] * 900)
    _write_plan(500)
    earlier = [f"MS-T2{i:07d}-RED-S" for i in range(120)]
    with open(up.NEW_ITEMS_LOG, "w", encoding="utf-8") as f:
        json.dump({"date": TODAY, "new_offer_ids": earlier}, f)
    up.main([])
    assert len(ozon.submitted) == 380
    assert len(_deferred_ids()) == 520
    assert _logged_ids() == earlier + ozon.submitted  # accumulates, accepted only


def test_quota_lower_than_plan_wins(ozon):
    _write_products([TYPE_ID_TOP] * 50)
    _write_plan(500)
    ozon.quota_remaining = 10
    up.main([])
    assert len(ozon.submitted) == 10
    assert len(_deferred_ids()) == 40


def test_quota_none_still_applies_plan(ozon):
    _write_products([TYPE_ID_TOP] * 900)
    _write_plan(500)
    ozon.quota_remaining = None
    up.main([])
    assert len(ozon.submitted) == 500
    assert len(_deferred_ids()) == 400


def test_per_category_allocations(ozon, capsys):
    types = [TYPE_ID_TOP] * 300 + [TYPE_ID_PAJAMA] * 200 + [TYPE_ID_SOCKS] * 200
    _write_products(types)
    _write_plan(250, {"M&S": {"Pajama": 120, "Socks": 80}}, fill=False)
    up.main([])
    by_type = {}
    for batch in ozon.imports:
        for it in batch:
            by_type[it["type_id"]] = by_type.get(it["type_id"], 0) + 1
    assert by_type == {TYPE_ID_PAJAMA: 120, TYPE_ID_SOCKS: 80}
    out = capsys.readouterr().out
    assert f"Plan {TODAY}: 250/day, 0 created so far; Pajama 120 left, Socks 80 left" in out
    assert "Capping at 250 (plan) / 2000 (quota)" in out


def test_allocations_with_fill_remainder(ozon):
    types = [TYPE_ID_TOP] * 300 + [TYPE_ID_PAJAMA] * 200
    _write_products(types)
    _write_plan(250, {"M&S": {"Pajama": 120}}, fill=True)
    up.main([])
    pajamas = [i for i, t in enumerate(types) if t == TYPE_ID_PAJAMA][:120]
    tops = list(range(130))
    assert sorted(ozon.submitted) == sorted(_offer_id(i) for i in pajamas + tops)


def test_updates_never_capped(ozon):
    _write_products([TYPE_ID_TOP] * 20)
    _write_plan(0)
    ozon.existing = {_offer_id(i) for i in range(10)}
    up.main([])
    assert ozon.submitted == [_offer_id(i) for i in range(10)]
    assert len(_deferred_ids()) == 10
    with pytest.raises(FileNotFoundError):
        _logged_ids()


def test_topup_run_creates_nothing_once_plan_used(ozon):
    _write_products([TYPE_ID_TOP] * 900)
    _write_plan(500)
    up.main([])
    first = list(ozon.submitted)
    ozon.existing |= set(first)  # created at 4am -> updates on the 5am run
    ozon.imports.clear()
    ozon.quota_remaining = 1500  # Ozon quota left, but the plan's 500 is used
    up.main([])
    new_submitted = [o for o in ozon.submitted if o not in ozon.existing]
    assert new_submitted == []
    assert sorted(ozon.submitted) == sorted(first)  # only updates went out
    assert _deferred_ids() == [_offer_id(i) for i in range(500, 900)]
    assert _logged_ids() == first


def test_deferred_items_go_first_next_run(ozon):
    _write_products([TYPE_ID_TOP] * 20)
    _write_plan(5)
    with open(up.DEFERRED_LOG, "w", encoding="utf-8") as f:
        json.dump([{"offer_id": _offer_id(i)} for i in (17, 18, 19)], f)
    up.main([])
    assert ozon.submitted[:3] == [_offer_id(17), _offer_id(18), _offer_id(19)]
    assert len(ozon.submitted) == 5
    assert _offer_id(17) not in _deferred_ids()


def test_dry_run_submits_and_writes_nothing(ozon, capsys, tmp_path):
    _write_products([TYPE_ID_TOP] * 30)
    _write_plan(10)
    with open(up.DEFERRED_LOG, "w", encoding="utf-8") as f:
        json.dump([{"offer_id": _offer_id(29)}], f)
    before = sorted(p.name for p in tmp_path.iterdir())
    up.main(["--dry-run"])
    assert ozon.imports == []
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # no new log/skip files
    assert _deferred_ids() == [_offer_id(29)]  # not consumed
    out = capsys.readouterr().out
    assert f"Plan {TODAY}: 10/day" in out
    assert "Capping at 10 (plan) / 2000 (quota)" in out
    assert "would submit 10 new" in out
