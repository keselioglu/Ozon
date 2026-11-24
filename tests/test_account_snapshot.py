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


# ---------------------------------------------------------------- account_snapshot.py

import os
from pathlib import Path

import pytest

import account_snapshot as snap_mod
from warehouse_routing import REGULAR_WAREHOUSE_ID, SMALL_WAREHOUSE_ID

CATALOG = {"built": "2026-11-24T04:10:00",
           "brands": {"M&S": {"live": 7, "for_sale": 5, "url_ok": 6},
                      "H&M": {"live": 2, "for_sale": 1},
                      "Other": {"live": 1, "for_sale": 0}},
           "total": {"live": 10, "for_sale": 6}}

LIVE = ["MS-T60002343-ROSEQUARTZ-38", "MS-T60002343-ROSEQUARTZ-40", "MS-T60002343-ROSEQUARTZ-42",
        "MS-T60002343-NAVY-38", "MS-T61000001X-BLACK-M",
        "MS-10000000314645-34", "MAR-T60002343-NAVY-40",
        "HM-0821677001", "1-HM-0821677001-RED-L", "ZZ-1"]

ASSIGN = {"MS-T60002343-ROSEQUARTZ-38": SMALL_WAREHOUSE_ID,
          "MS-T60002343-ROSEQUARTZ-40": str(REGULAR_WAREHOUSE_ID),
          "MS-T60002343-NAVY-38": 123,
          "HM-0821677001": str(SMALL_WAREHOUSE_ID),
          "NOT-LIVE-1": REGULAR_WAREHOUSE_ID}

MISSING = object()  # sentinel: don't create the file


def _live_doc(ids=LIVE):
    return {"built": "2026-11-24T04:10:00", "count": len(ids), "offer_ids": ids}


def _build(cs=CATALOG, live=None, asg=ASSIGN, sources=None):
    live = _live_doc() if live is None else live
    return snap_mod.build_snapshot(cs, live, asg, sources, now=snap_mod.datetime(2026, 11, 24, 9, 0, 0))


def test_offers_and_for_sale_from_catalog_status():
    s = _build()
    for b in snap_mod.BRANDS:
        assert s["brands"][b]["offers"] == CATALOG["brands"][b]["live"]
        assert s["brands"][b]["for_sale"] == CATALOG["brands"][b]["for_sale"]
    assert s["total"]["offers"] == 10 and s["total"]["for_sale"] == 6
    assert s["version"] == 1 and s["built_at"] == "2026-11-24T09:00:00"


def test_total_summed_when_missing_and_null_for_bad_types():
    cs = {"brands": {"M&S": {"live": 3, "for_sale": 1}, "H&M": {"live": 2, "for_sale": 2},
                     "Other": {"live": 0, "for_sale": 0}}}
    s = _build(cs=cs)
    assert s["total"]["offers"] == 5 and s["total"]["for_sale"] == 3
    cs = {"brands": {"M&S": {"live": "x", "for_sale": 1}}, "total": {"live": 9}}
    s = _build(cs=cs)
    assert s["brands"]["M&S"]["offers"] is None and s["brands"]["M&S"]["for_sale"] == 1
    assert s["brands"]["H&M"]["offers"] is None
    assert s["total"]["offers"] == 9 and s["total"]["for_sale"] is None


def test_products_articles_sizes_unparsed():
    s = _build()
    ms = s["brands"]["M&S"]
    assert ms["live_ids_count"] == 7
    assert ms["products"] == 3 and ms["articles"] == 2
    assert ms["unparsed"] == 2
    assert ms["sizes"] == {"38": 2, "40": 1, "42": 1, "M": 1}
    hm = s["brands"]["H&M"]
    assert hm["live_ids_count"] == 2 and hm["unparsed"] == 2
    assert hm["products"] == 0 and hm["articles"] == 0 and hm["sizes"] == {}
    assert s["brands"]["Other"]["unparsed"] == 1
    t = s["total"]
    assert t["live_ids_count"] == 10 and t["products"] == 3 and t["articles"] == 2
    assert t["unparsed"] == 5 and t["sizes"] == {"38": 2, "40": 1, "42": 1, "M": 1}


def test_parse_offer_id_strict():
    assert snap_mod.parse_offer_id("MS-T60000001-ROSE-QUARTZ-38") == {
        "article": "T60000001", "color": "ROSE-QUARTZ", "size": "38"}
    assert snap_mod.parse_offer_id("MS-T61000001X-BLACK-M")["article"] == "T61000001X"
    for bad in ("MS-10000000314645-34", "MAR-T60002343-NAVY-40", "SML-MAR-T60002343-NAVY-40",
                "MARK-T60002343-NAVY-40", "MARKS-T60002343-NAVY-40", "HM-0821677001",
                "1-HM-0821677001-RED-L", "MS-T60002343-NAVY", "MS-T60002343--38",
                "MS-T1234-NAVY-38", "MS-T60002343ABC-NAVY-38", "", None, 5):
        assert snap_mod.parse_offer_id(bad) is None, bad


def test_warehouse_labels():
    assert snap_mod.warehouse_label(SMALL_WAREHOUSE_ID) == "Small items"
    assert snap_mod.warehouse_label(str(SMALL_WAREHOUSE_ID)) == "Small items"
    assert snap_mod.warehouse_label(REGULAR_WAREHOUSE_ID) == "Regular"
    assert snap_mod.warehouse_label(str(REGULAR_WAREHOUSE_ID)) == "Regular"
    assert snap_mod.warehouse_label(None) == "Unassigned"
    assert snap_mod.warehouse_label(123) == "Other 123"
    assert snap_mod.warehouse_label("123") == "Other 123"
    assert snap_mod.warehouse_label("abc") == "Other abc"


def test_offers_by_warehouse():
    w = _build()["offers_by_warehouse"]
    assert w["total"] == {"Small items": 2, "Regular": 1, "Other 123": 1, "Unassigned": 6}
    assert w["by_brand"]["M&S"] == {"Small items": 1, "Regular": 1, "Other 123": 1, "Unassigned": 4}
    assert w["by_brand"]["H&M"] == {"Small items": 1, "Unassigned": 1}
    summed = {}
    for b in snap_mod.BRANDS:
        for k, v in w["by_brand"][b].items():
            summed[k] = summed.get(k, 0) + v
    assert summed == w["total"]
    assert sum(w["total"].values()) == len(LIVE)  # live ids only, NOT-LIVE-1 not counted


def test_missing_assignments_all_unassigned():
    s = _build(asg=None)
    assert s["offers_by_warehouse"]["total"] == {"Unassigned": len(LIVE)}
    assert s["sources"]["warehouse_assignments"]["count"] is None


@pytest.mark.parametrize("live", [MISSING, ["MS-T60002343-NAVY-38"], "garbage", {"offer_ids": "x"}, 5, {"no": "ids"}])
def test_bad_live_ids_give_nulls(live):
    s = snap_mod.build_snapshot(CATALOG, None if live is MISSING else live, ASSIGN, None)
    for b in snap_mod.BRANDS + ("total",):
        x = s["total"] if b == "total" else s["brands"][b]
        for k in ("products", "articles", "unparsed", "sizes", "live_ids_count"):
            assert x[k] is None, (b, k)
    assert s["offers_by_warehouse"] is None
    assert s["brands"]["M&S"]["offers"] == 7


def test_empty_live_list_is_zero_not_null():
    s = _build(live=_live_doc([]))
    assert s["total"]["live_ids_count"] == 0 and s["total"]["products"] == 0
    assert s["offers_by_warehouse"]["total"] == {}


def test_bad_types_inside_inputs_tolerated():
    s = snap_mod.build_snapshot({"brands": ["x"], "total": "y"},
                                {"offer_ids": ["MS-T60002343-NAVY-38", 5, None, {"a": 1}]},
                                ["not", "a", "dict"], "not a dict")
    assert s["brands"]["M&S"]["offers"] is None
    assert s["total"]["offers"] is None
    assert s["brands"]["M&S"]["products"] == 1 and s["total"]["live_ids_count"] == 1
    assert s["offers_by_warehouse"]["total"] == {"Unassigned": 1}
    snap_mod.format_summary(s)
    s = snap_mod.build_snapshot(None, None, None, None)
    assert s["brands"]["M&S"]["offers"] is None
    assert "unknown" in snap_mod.format_summary(s)


def _write_inputs(tmp_path, cs=CATALOG, live=None, asg=ASSIGN):
    live = _live_doc() if live is None else live
    paths = {}
    for name, data in (("cs.json", cs), ("live.json", live), ("wh.json", asg)):
        p = tmp_path / name
        if isinstance(data, bytes):
            p.write_bytes(data)
        elif data is not MISSING:
            p.write_text(json.dumps(data), encoding="utf-8")
        paths[name] = str(p)
    return ["--catalog-status", paths["cs.json"], "--live-ids", paths["live.json"],
            "--warehouses", paths["wh.json"]]


def test_main_writes_valid_json(tmp_path, capsys):
    out = tmp_path / "out" / "snap.json"
    out.parent.mkdir()
    assert snap_mod.main(_write_inputs(tmp_path) + ["--out", str(out)]) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["version"] == 1
    assert d["sources"]["catalog_status"]["status"] == "ok"
    assert d["sources"]["catalog_status"]["built"] == CATALOG["built"]
    assert d["sources"]["live_offer_ids"]["built"] == "2026-11-24T04:10:00"
    assert d["sources"]["warehouse_assignments"]["count"] == len(ASSIGN)
    assert d["sources"]["catalog_status"]["mtime"]
    assert d["brands"]["M&S"]["products"] == 3
    assert os.listdir(out.parent) == ["snap.json"]
    assert "M&S" in capsys.readouterr().out


def test_main_print_writes_nothing(tmp_path):
    args = _write_inputs(tmp_path)
    out = tmp_path / "snap.json"
    before = sorted(os.listdir(tmp_path))
    assert snap_mod.main(["--print", "--out", str(out)] + args) == 0
    assert not out.exists()
    assert sorted(os.listdir(tmp_path)) == before


@pytest.mark.parametrize("which", ["cs", "live", "wh"])
@pytest.mark.parametrize("kind,status", [(MISSING, "missing"), (b"\xff\xfe garbage {[", "corrupt"),
                                         (["a", "list"], "corrupt")])
def test_main_bad_inputs(tmp_path, capsys, which, kind, status):
    kw = {"cs": CATALOG, "live": None, "asg": ASSIGN}
    kw[{"cs": "cs", "live": "live", "wh": "asg"}[which]] = kind
    args = _write_inputs(tmp_path, **kw)
    assert snap_mod.main(["--print"] + args) == 0
    out = tmp_path / "snap.json"
    assert snap_mod.main(["--out", str(out)] + args) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    key = {"cs": "catalog_status", "live": "live_offer_ids", "wh": "warehouse_assignments"}[which]
    assert d["sources"][key]["status"] == status
    if which == "cs":
        assert d["brands"]["M&S"]["offers"] is None and d["total"]["offers"] is None
        assert d["brands"]["M&S"]["products"] == 3
    elif which == "live":
        assert d["brands"]["M&S"]["products"] is None and d["offers_by_warehouse"] is None
        assert d["brands"]["M&S"]["offers"] == 7
    else:
        assert d["offers_by_warehouse"]["total"] == {"Unassigned": len(LIVE)}
    assert "failed" not in capsys.readouterr().err


def test_load_json_safe(tmp_path):
    assert snap_mod.load_json_safe(tmp_path / "nope.json") == (None, "missing")
    p = tmp_path / "bad.json"
    p.write_bytes(b"\xff\xfe\x00garbage")
    assert snap_mod.load_json_safe(p) == (None, "corrupt")
    assert snap_mod.load_json_safe(tmp_path) == (None, "corrupt")  # a directory
    p.write_text('{"a": 1}', encoding="utf-8")
    assert snap_mod.load_json_safe(p) == ({"a": 1}, "ok")
    assert snap_mod.file_timestamp(p)
    assert snap_mod.file_timestamp(tmp_path / "nope.json") is None


def test_write_atomic_uses_tmp_in_same_dir(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        real(src, dst)

    monkeypatch.setattr(snap_mod.os, "replace", spy)
    out = tmp_path / "snap.json"
    snap_mod.write_atomic(str(out), {"version": 1, "x": "ü"})
    assert len(calls) == 1
    src, dst = calls[0]
    assert Path(src).parent == tmp_path and Path(src) != out and Path(dst) == out
    assert json.loads(out.read_text(encoding="utf-8")) == {"version": 1, "x": "ü"}
    assert os.listdir(tmp_path) == ["snap.json"]


def test_write_atomic_failure_keeps_existing(tmp_path):
    out = tmp_path / "snap.json"
    out.write_text('{"old": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        snap_mod.write_atomic(str(out), {"bad": object()})
    assert json.loads(out.read_text(encoding="utf-8")) == {"old": True}
    assert os.listdir(tmp_path) == ["snap.json"]


def test_source_has_no_network_code():
    src = Path(snap_mod.__file__).read_text(encoding="utf-8")
    for bad in ("ozon_client", "requests", "urllib", "http", "call(", "live_offer_ids("):
        assert bad not in src, bad
