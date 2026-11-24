"""Offline tests: category_inventory.py aggregation, history and file safety."""
import json

import pytest

import category_inventory as ci
import created_history
import daily_plan
from ozon_mapping import TYPE_ID_PAJAMA, TYPE_ID_SOCKS

TODAY = "2026-11-24"


def _label(name):
    name = (name or "").lower()
    if "pijama" in name:
        return "Pajama"
    if "çorap" in name:
        return "Socks"
    return None


def _row(code, name, color, size, stock=3, psku="10000000000001"):
    return {"ms_article_code": code, "parent_sku": psku, "name": name, "color": color,
            "size_label": size, "stock_count": str(stock), "url": f"https://x/{code}"}


@pytest.fixture
def inputs():
    rows = [
        # pajama: two sizes, one already live, one deferred, plus a duplicate row
        _row("T37000001", "Pijama Takımı", "NAVY", "34 (UK 6)"),
        _row("T37000001", "Pijama Takımı", "NAVY", "36 (UK 8)"),
        _row("T37000001", "Pijama Takımı", "NAVY", "38 (UK 10)"),
        _row("T37000001", "Pijama Takımı", "NAVY", "38 (UK 10)"),
        _row("T37000001", "Pijama Takımı", "NAVY", "40 (UK 12)", stock=0),
        # socks: one in-stock size, not live
        _row("T38000002", "Pamuklu Çorap", "BLACK", "35.5-38 (UK 3-5)"),
        # unmapped name
        _row("T99000003", "Şemsiye", "RED", "34 (UK 6)"),
    ]
    oid = ci.row_offer_id
    live_pj = oid(rows[0])
    deferred_pj = oid(rows[1])
    live = [live_pj, "MS-10000000000001-40", "MS-T11111111-RED-M", "HM-123-RED-S"]
    deferred = [{"offer_id": deferred_pj, "type_id": TYPE_ID_PAJAMA},
                {"offer_id": "MS-T38000009-BLACK-35", "type_id": TYPE_ID_SOCKS},
                {"offer_id": "MS-X", "type_id": 1}]
    skips = [{"date": TODAY, "sku": 1, "name": "Pijama Takımı"},
             {"date": TODAY, "sku": 1, "name": "Pijama Takımı"},
             {"date": TODAY, "sku": 2, "name": "Pamuklu Çorap"}]
    discovery = {"date": "2026-11-23", "counts": {"M&S": {"Socks": {"products": 4, "offers": 9},
                                                          "Unmapped": {"products": 2, "offers": 2}}}}
    sections = [{"name": "Çorap", "url": "https://x/corap/", "priority": "1"},
                {"name": "Giyim (Tümü)", "url": "https://x/giyim/", "priority": "2"}]
    return dict(live_ids=live, product_rows=rows, deferred_items=deferred, skip_records=skips,
                discovery=discovery, hm_status={"live": 1273, "for_sale": 310, "url_ok": 1270},
                site_sections=sections, label_for_name=_label, today=TODAY)


def test_aggregate_counts(inputs):
    data = ci.aggregate(**inputs)
    ms = data["brands"]["M&S"]
    cats = ms["categories"]
    assert set(cats) == set(daily_plan.CATEGORY_LABELS.values())
    pj, socks = cats["Pajama"], cats["Socks"]
    # live: the article-coded one + the legacy parent-sku one
    assert pj["live_offers"] == 2
    assert ms["live"] == 3 and ms["live_unlabelled"] == 1
    # 34 live, 36 deferred, 38 counted once (duplicate row), 40 out of stock
    assert pj["crawled_not_live_offers"] == 1
    assert pj["deferred_offers"] == 1 and socks["deferred_offers"] == 1
    assert ms["deferred_unlabelled"] == 1
    assert socks["crawled_not_live_offers"] == 1
    assert ms["unmapped_crawled"] == 1
    assert pj["upload_skips_7d"] == 1 and socks["upload_skips_7d"] == 1
    assert socks["discovered_not_live_products"] == 4
    assert socks["discovered_not_live_offers_est"] == 9
    assert socks["last_discovery_walk"] == "2026-11-23"
    assert ms["discovered_unmapped_products"] == 2
    assert [s["name"] for s in socks["site_sections"]] == ["Çorap"]
    assert cats["Dress"]["live_offers"] == 0
    hm = data["brands"]["H&M"]
    assert hm["add_pipeline"] is False and hm["live"] == 1273 and hm["for_sale"] == 310
    json.dumps(data)  # serialisable


def test_aggregate_excludes_submitted_today(inputs):
    sock_oid = ci.row_offer_id(inputs["product_rows"][5])
    data = ci.aggregate(**inputs, new_items_log={"date": TODAY, "new_offer_ids": [sock_oid]})
    assert data["brands"]["M&S"]["categories"]["Socks"]["crawled_not_live_offers"] == 0
    today = data["created_history"][-1]
    assert today["date"] == TODAY and today["created_total"] == 1
    assert today["by_category"] == {"Socks": 1}


def test_aggregate_empty_inputs():
    data = ci.aggregate(live_ids=[], product_rows=None, deferred_items=None, skip_records=None,
                        discovery=None, hm_status=None, site_sections=[], label_for_name=_label,
                        today=TODAY)
    assert set(data["brands"]["M&S"]["categories"]) == set(daily_plan.CATEGORY_LABELS.values())
    assert data["brands"]["H&M"]["add_pipeline"] is False
    assert len(data["created_history"]) == 14


def test_created_history_sources():
    hist = ci.build_created_history(
        TODAY,
        created_log={"2026-11-20": {"created_total": 480, "by_category": {"Pajama": 480}}},
        report_added={"2026-11-20": 300, "2026-11-19": 250},
        new_items_log=None, article_labels={})
    assert [h["date"] for h in hist][0] == "2026-11-11" and hist[-1]["date"] == TODAY
    by_date = {h["date"]: h for h in hist}
    assert by_date["2026-11-20"]["created_total"] == 480
    assert by_date["2026-11-20"]["source"] == "daily_created_history"
    assert by_date["2026-11-19"]["created_total"] == 250
    assert by_date["2026-11-19"]["by_category"] is None
    assert by_date["2026-11-18"]["created_total"] is None


def test_record_snapshot_dedupes_and_never_shrinks(tmp_path):
    path = tmp_path / "h.jsonl"
    labels = {"T07000001": "Pajama", "T41000002": "Socks"}
    log = {"date": TODAY, "new_offer_ids": ["MS-T07000001-BLACK-M", "MS-T07000001-BLACK-L"]}
    assert created_history.record_snapshot(log, labels, path=path)["created_total"] == 2
    assert created_history.record_snapshot(log, labels, path=path) is None  # unchanged
    log["new_offer_ids"] += ["MS-T41000002-NAVY-38", "MS-T99999999-RED-S"]
    created_history.record_snapshot(log, labels, path=path)
    rec = created_history.load_history(path)[TODAY]
    assert rec["created_total"] == 4
    assert rec["by_category"] == {"Pajama": 2, "Socks": 1, "Unmapped": 1}
    assert created_history.record_snapshot({"date": TODAY, "new_offer_ids": []}, labels, path=path) is None
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_recent_skip_records_dated_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [{"sku": 1, "name": "old, no date"},
             {"date": "2026-11-10", "sku": 2},
             {"date": "2026-11-20", "sku": 3},
             {"date": TODAY, "sku": 4}]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    recs = ci.recent_skip_records(path, today=TODAY)
    assert [r["sku"] for r in recs] == [4, 3]
    # small block size exercises the partial-line stitching
    assert [r["sku"] for r in ci.iter_jsonl_reverse(path, block=7)] == [4, 3, 2, 1]


def test_recent_skip_records_undated_uses_offsets(tmp_path):
    path = tmp_path / "s.jsonl"
    old = "".join(json.dumps({"sku": i}) + "\n" for i in range(3))
    new = "".join(json.dumps({"sku": i}) + "\n" for i in range(10, 13))
    path.write_bytes((old + new).encode("utf-8"))
    off = len(old.encode("utf-8"))
    for block in (5, 1 << 20):
        pos = [p for p, _ in ci.iter_jsonl_reverse(path, block=block, with_pos=True)]
        assert pos[-1] == 0 and pos[2] == off
    offsets = {"2026-11-01": 0, "2026-11-20": off}  # 11-01 is outside the 7-day window
    recs = ci.recent_skip_records(path, today=TODAY, offsets=offsets)
    assert sorted(r["sku"] for r in recs) == [10, 11, 12]
    # CRLF lines (upload_to_ozon appends in text mode on Windows)
    path.write_bytes((old + new).replace("\n", "\r\n").encode("utf-8"))
    crlf_off = len(old.replace("\n", "\r\n").encode("utf-8"))
    recs = ci.recent_skip_records(path, today=TODAY, offsets={"2026-11-20": crlf_off})
    assert sorted(r["sku"] for r in recs) == [10, 11, 12]
    assert ci.skip_window_start(offsets, TODAY) == ("2026-11-20", off)
    assert ci.recent_skip_records(path, today=TODAY, offsets={}) == []


def test_record_history_writes_only_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "upload_skipped.jsonl").write_bytes(b'{"sku": 1}\r\n')  # text-mode append on Windows
    (tmp_path / "new_items_submitted.json").write_text(
        json.dumps({"date": TODAY, "new_offer_ids": ["MS-T07000001-BLACK-M"]}), encoding="utf-8")
    assert ci.record_history(today=TODAY) == 0
    assert ci.record_history(today=TODAY) == 0
    offsets = json.loads((tmp_path / "upload_skipped_offsets.json").read_text(encoding="utf-8"))
    assert offsets == {TODAY: 12}
    assert created_history.load_history(tmp_path / "daily_created_history.jsonl")[TODAY]["created_total"] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "daily_created_history.jsonl", "new_items_submitted.json",
        "upload_skipped.jsonl", "upload_skipped_offsets.json"]


def test_main_writes_only_inventory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "product_urls.txt").write_text("https://x/a\n", encoding="utf-8")
    (tmp_path / "deferred_items.json").write_text("[]", encoding="utf-8")
    (tmp_path / "category_inventory.json").write_text(
        json.dumps({"discovery_found": {"date": "2026-11-23", "counts": {}}}), encoding="utf-8")
    monkeypatch.setattr(ci, "live_offer_ids", lambda: [])
    monkeypatch.setattr(ci, "read_quota", lambda: {"limit": 2000, "usage": 10, "remaining": 1990})
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != "category_inventory.json"}
    assert ci.main([]) == 0
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != "category_inventory.json"}
    assert before == after  # nothing else written, marker removed
    data = json.loads((tmp_path / "category_inventory.json").read_text(encoding="utf-8"))
    assert data["discovery_found"]["date"] == "2026-11-23"
    assert data["brands"]["H&M"]["add_pipeline"] is False
    assert data["sources"]["products_csv"].startswith("missing")


def test_walk_queues_nothing(tmp_path, monkeypatch):
    import category_priority as cp
    monkeypatch.chdir(tmp_path)
    (tmp_path / "product_urls.txt").write_text("https://x/queued\n", encoding="utf-8")
    monkeypatch.setattr(cp, "load_priority_categories", lambda path=None: [
        {"Category Name (TR)": "Çorap", "Full URL": "https://x/corap/", "Proprity": 1},
        {"Category Name (TR)": "Çorap 2", "Full URL": "https://x/corap2/", "Proprity": 2}])
    monkeypatch.setattr(cp, "is_supported_category", lambda name, url=None: True)
    monkeypatch.setattr(cp, "fetch_live_ms_identifiers", lambda: set())
    monkeypatch.setattr(cp, "discover_category_products",
                        lambda url: {"https://x/queued", "https://x/s1", "https://x/s2"})
    import crawler
    monkeypatch.setattr(crawler, "extract_product", lambda url: [
        {"name": "Çorap", "ms_article_code": url[-2:], "parent_sku": None, "stock_count": 2}])
    disc = ci.walk_discovery(_label)
    assert disc["counts"]["M&S"]["Socks"] == {"products": 2, "offers": 2}  # deduped across sections
    assert (tmp_path / "product_urls.txt").read_text(encoding="utf-8") == "https://x/queued\n"
