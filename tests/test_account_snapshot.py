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
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import account_snapshot as snap_mod
from warehouse_routing import REGULAR_WAREHOUSE_ID, SMALL_WAREHOUSE_ID

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 24, 9, 0, 0)


def _ago(**kw):
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


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

PRICES = {"built": "2026-11-24T04:10:00", "count": 6, "prices": {
    "MS-T60002343-ROSEQUARTZ-38": 20.0,       # Small
    "HM-0821677001": 10.5,                  # Small
    "MS-T60002343-ROSEQUARTZ-40": 75.5,       # Regular
    "MS-T60002343-NAVY-38": 0,                # Other 123: invalid
    "MS-T60002343-ROSEQUARTZ-42": 30,         # Unassigned
    "MS-T61000001X-BLACK-M": "40",            # invalid type
    "ZZ-1": 12.3,                           # Unassigned
    "NOT-LIVE-1": 999.0}}                     # not live: ignored

STOCK_HIST = [
    {"finished_at": _ago(hours=48), "products_pushed_ok": 1},
    {"finished_at": _ago(hours=5), "products_pushed_ok": 4},
    {"finished_at": "garbage", "products_pushed_ok": 6},
    {"finished_at": _ago(hours=1), "products_pushed_ok": "x"},
]
PRICE_HIST = [{"date": "2026-11-23", "finished_at": _ago(hours=3), "pushed_ok": 100}]
HM_RUN = {"date": _ago(hours=7), "decisions": {
    "HM-0821677001": {"stock": 5, "why": "in_stock"},
    "1-HM-0821677001-RED-L": {"why": "product_gone"},
    "HM-NOT-LIVE": {"stock": 3, "why": "in_stock"}}}
METRICS = {"version": 1, "built_at": "2026-11-24T10:00:01+03:00",
           "campaigns": {"live_skus": 12, "ratio_count": 10, "avg_ratio_pct": 31.2,
                         "min_ratio_pct": 20.0, "max_ratio_pct": 46.0},
           "sales": {"orders": 3, "units": 4, "revenue_usd": 81.5, "top_category": None}}

MISSING = object()  # sentinel: don't create the file
MISSING_LIVE = "not a live doc"


def _live_doc(ids=LIVE):
    return {"built": "2026-11-24T04:10:00", "count": len(ids), "offer_ids": ids}


def _build(cs=CATALOG, live=None, asg=ASSIGN, sources=None, now=NOW, **kw):
    live = _live_doc() if live is None else live
    full = {"prices": PRICES, "overview_metrics": METRICS, "stock_history": STOCK_HIST,
            "price_history": PRICE_HIST, "hm_last_run": HM_RUN}
    full.update(kw)
    return snap_mod.build_snapshot(cs, live, asg, sources, now=now, **full)


def test_offers_and_for_sale_from_catalog_status():
    s = _build()
    for b in snap_mod.BRANDS:
        assert s["brands"][b]["offers"] == CATALOG["brands"][b]["live"]
        assert s["brands"][b]["for_sale"] == CATALOG["brands"][b]["for_sale"]
    assert s["total"]["offers"] == 10 and s["total"]["for_sale"] == 6
    assert s["version"] == 2 and s["built_at"] == "2026-11-24T09:00:00"


def test_dropped_fields_are_gone():
    s = _build()
    for x in list(s["brands"].values()) + [s["total"]]:
        for k in ("products", "articles", "unparsed", "sizes"):
            assert k not in x
    assert not hasattr(snap_mod, "parse_offer_id")


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


def test_up_to_date_counts():
    s = _build()
    ms, hm, other, total = (s["brands"]["M&S"], s["brands"]["H&M"], s["brands"]["Other"], s["total"])
    assert ms["stock_up_to_date"] == 4          # newest valid in-window line (5h old), bad lines skipped
    assert ms["price_up_to_date"] == 7          # 100 pushed, capped at M&S live 7
    assert hm["stock_up_to_date"] == 1          # only live H&M ids with a numeric stock
    assert hm["price_up_to_date"] == 0 and other["price_up_to_date"] == 0
    assert other["stock_up_to_date"] == 0
    assert total["stock_up_to_date"] == 5 and total["price_up_to_date"] == 7
    assert s["notes"]["price_up_to_date"] == {"H&M": "no price run", "Other": "no price run"}
    assert s["up_to_date"]["hours"] == snap_mod.UP_TO_DATE_HOURS == 24
    assert s["up_to_date"]["runs"]["stock_ms"] == {"finished_at": _ago(hours=5), "ok": 4}
    assert s["up_to_date"]["runs"]["stock_hm"]["fresh"] is True


@pytest.mark.parametrize("age,counted", [(timedelta(hours=23, minutes=59), True),
                                         (timedelta(hours=24), True),
                                         (timedelta(hours=24, minutes=1), False)])
def test_24h_cutoff(age, counted):
    when = (NOW - age).isoformat(timespec="seconds")
    s = _build(stock_history=[{"finished_at": when, "products_pushed_ok": 3}],
               price_history=[{"finished_at": when, "pushed_ok": 2}],
               hm_last_run={"date": when, "decisions": {"HM-0821677001": {"stock": 0}}})
    b = s["brands"]
    assert b["M&S"]["stock_up_to_date"] == (3 if counted else 0)
    assert b["M&S"]["price_up_to_date"] == (2 if counted else 0)
    assert b["H&M"]["stock_up_to_date"] == (1 if counted else 0)


def test_newest_line_in_window_wins_regardless_of_order():
    hist = [{"finished_at": _ago(hours=2), "products_pushed_ok": 6},
            {"finished_at": _ago(hours=10), "products_pushed_ok": 2}]
    assert _build(stock_history=hist)["brands"]["M&S"]["stock_up_to_date"] == 6


def test_cap_at_live_count_and_fallback_to_live_ids():
    s = _build(stock_history=[{"finished_at": _ago(hours=1), "products_pushed_ok": 12000}])
    assert s["brands"]["M&S"]["stock_up_to_date"] == 7
    s = _build(cs=None, stock_history=[{"finished_at": _ago(hours=1), "products_pushed_ok": 12000}])
    assert s["brands"]["M&S"]["stock_up_to_date"] == 7  # 7 M&S-family live ids
    s = _build(cs=None, live=MISSING_LIVE,
               stock_history=[{"finished_at": _ago(hours=1), "products_pushed_ok": 3}])
    assert s["brands"]["M&S"]["stock_up_to_date"] is None  # can't cap: unknown



def test_up_to_date_null_vs_zero():
    s = _build(stock_history=None, price_history=None, hm_last_run=None)
    assert s["brands"]["M&S"]["stock_up_to_date"] is None
    assert s["brands"]["M&S"]["price_up_to_date"] is None
    assert s["brands"]["H&M"]["stock_up_to_date"] is None
    assert s["total"]["stock_up_to_date"] is None and s["total"]["price_up_to_date"] is None
    s = _build(stock_history=[], price_history=[{"finished_at": _ago(days=3), "pushed_ok": 5}],
               hm_last_run={"date": "garbage"})
    assert s["brands"]["M&S"]["stock_up_to_date"] == 0
    assert s["brands"]["M&S"]["price_up_to_date"] == 0
    assert s["brands"]["H&M"]["stock_up_to_date"] == 0
    assert s["total"]["stock_up_to_date"] == 0


def test_hm_fresh_but_no_live_ids_is_null():
    s = _build(live=MISSING_LIVE)
    assert s["brands"]["H&M"]["stock_up_to_date"] is None
    assert s["total"]["stock_up_to_date"] is None


def test_tz_aware_and_date_only_stamps():
    aware = (NOW - timedelta(hours=2)).astimezone().isoformat()
    s = _build(stock_history=[{"finished_at": aware, "products_pushed_ok": 2}])
    assert s["brands"]["M&S"]["stock_up_to_date"] == 2
    s = _build(price_history=[{"finished_at": "2026-11-23", "pushed_ok": 2}])  # end of day, 9h ago
    assert s["brands"]["M&S"]["price_up_to_date"] == 2
    s = _build(price_history=[{"finished_at": "2030-01-01T00:00:00", "pushed_ok": 2}])  # broken clock
    assert s["brands"]["M&S"]["price_up_to_date"] == 0


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


def test_warehouses_price_stats():
    w = _build()["warehouses"]
    assert set(w) == {"Small items", "Regular", "Other 123", "Unassigned"}
    small = w["Small items"]
    assert small["skus"] == 2 and small["by_brand"] == {"M&S": 1, "H&M": 1, "Other": 0}
    assert small["price_count"] == 2
    assert small["avg_price"] == 15.25 and small["min_price"] == 10.5
    assert small["max_price"] == 20.0
    assert w["Regular"] == {"skus": 1, "by_brand": {"M&S": 1, "H&M": 0, "Other": 0},
                            "price_count": 1, "avg_price": 75.5, "min_price": 75.5, "max_price": 75.5}
    assert w["Other 123"]["price_count"] == 0 and w["Other 123"]["avg_price"] is None
    un = w["Unassigned"]
    assert un["skus"] == 6 and un["price_count"] == 2  # "40" string and missing ones skipped
    assert un["avg_price"] == 21.15 and un["min_price"] == 12.3
    assert un["max_price"] == 30.0


def test_warehouses_null_prices_without_price_file():
    for prices in (None, {"prices": "x"}, ["list"]):
        w = _build(prices=prices)["warehouses"]
        for e in w.values():
            assert e["price_count"] is None and e["avg_price"] is None
            assert e["min_price"] is None and e["max_price"] is None
            assert e["skus"] > 0


def test_missing_assignments_all_unassigned():
    s = _build(asg=None)
    assert s["offers_by_warehouse"]["total"] == {"Unassigned": len(LIVE)}
    assert s["warehouses"]["Unassigned"]["skus"] == len(LIVE)
    assert s["warehouses"]["Unassigned"]["price_count"] == 5
    assert s["sources"]["warehouse_assignments"]["count"] is None


def test_campaigns_sales_copied_through():
    s = _build()
    assert s["campaigns"] == METRICS["campaigns"] and s["sales"] == METRICS["sales"]
    assert s["metrics_built_at"] == METRICS["built_at"]
    s = _build(overview_metrics={"built_at": "x", "campaigns": None})
    assert s["campaigns"] is None and s["sales"] is None and s["metrics_built_at"] == "x"
    for bad in (None, ["x"], "y"):
        s = _build(overview_metrics=bad)
        assert s["campaigns"] is None and s["sales"] is None and s["metrics_built_at"] is None


@pytest.mark.parametrize("live", [MISSING, ["MS-T60002343-NAVY-38"], "garbage", {"offer_ids": "x"}, 5, {"no": "ids"}])
def test_bad_live_ids_give_nulls(live):
    s = snap_mod.build_snapshot(CATALOG, None if live is MISSING else live, ASSIGN, None)
    for b in snap_mod.BRANDS + ("total",):
        x = s["total"] if b == "total" else s["brands"][b]
        assert x["live_ids_count"] is None, b
    assert s["offers_by_warehouse"] is None and s["warehouses"] is None
    assert s["brands"]["M&S"]["offers"] == 7


def test_empty_live_list_is_zero_not_null():
    s = _build(live=_live_doc([]))
    assert s["total"]["live_ids_count"] == 0
    assert s["offers_by_warehouse"]["total"] == {} and s["warehouses"] == {}
    assert "Warehouses: none" in snap_mod.format_summary(s)


def test_bad_types_inside_inputs_tolerated():
    s = snap_mod.build_snapshot({"brands": ["x"], "total": "y"},
                                {"offer_ids": ["MS-T60002343-NAVY-38", 5, None, {"a": 1}]},
                                ["not", "a", "dict"], "not a dict", now="bad",
                                prices={"prices": {"MS-T60002343-NAVY-38": float("nan"), 5: 3}},
                                overview_metrics=5, stock_history=[1, "x", None, {"finished_at": 5}],
                                price_history="not a list",
                                hm_last_run={"date": _ago(hours=1), "decisions": ["x"]})
    assert s["brands"]["M&S"]["offers"] is None
    assert s["total"]["offers"] is None
    assert s["total"]["live_ids_count"] == 1
    assert s["offers_by_warehouse"]["total"] == {"Unassigned": 1}
    assert s["warehouses"]["Unassigned"]["price_count"] == 0
    snap_mod.format_summary(s)


def test_all_inputs_missing_is_valid_snapshot():
    s = snap_mod.build_snapshot(None, None, None, None)
    assert s["version"] == 2
    for x in list(s["brands"].values()) + [s["total"]]:
        assert x["offers"] is None and x["for_sale"] is None
    assert s["total"]["stock_up_to_date"] is None and s["total"]["price_up_to_date"] is None
    assert s["warehouses"] is None and s["campaigns"] is None and s["sales"] is None
    assert s["metrics_built_at"] is None
    json.dumps(s)
    assert "unknown" in snap_mod.format_summary(s)


def test_format_summary_mentions_new_fields():
    text = snap_mod.format_summary(_build())
    assert "stock up to date 4" in text and "price up to date 7" in text
    assert "Warehouse Small items: 2 SKUs, 2 priced, avg $15.25" in text
    assert "Campaigns: live SKUs 12" in text and "Sales: orders 3" in text


# --- jsonl tail reading ------------------------------------------------------------------

def test_load_jsonl_tail(tmp_path):
    p = tmp_path / "h.jsonl"
    assert snap_mod.load_jsonl_tail(p) == (None, "missing")
    p.write_bytes(b'{"a": 1}\nnot json\n{"cut": \n\n[1]\n{"a": 2}\n{"a": 3}')
    assert snap_mod.load_jsonl_tail(p) == ([{"a": 1}, {"a": 2}, {"a": 3}], "ok")
    # tail: the first (cut) line is dropped
    assert snap_mod.load_jsonl_tail(p, max_bytes=len('{"a": 2}\n{"a": 3}') + 3) == ([{"a": 2}, {"a": 3}], "ok")
    p.write_bytes(b"\xff\xfe garbage\nmore garbage\n")
    assert snap_mod.load_jsonl_tail(p) == (None, "corrupt")
    p.write_bytes(b"")
    assert snap_mod.load_jsonl_tail(p) == ([], "ok")
    assert snap_mod.load_jsonl_tail(tmp_path)[1] == "corrupt"  # a directory


# --- main() -----------------------------------------------------------------------------

def _write(p, data):
    if data is MISSING:
        return
    if isinstance(data, bytes):
        p.write_bytes(data)
    elif p.suffix == ".jsonl" and isinstance(data, list):
        p.write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")
    else:
        p.write_text(json.dumps(data), encoding="utf-8")


INPUTS = {"cs": ("--catalog-status", "cs.json", CATALOG),
          "live": ("--live-ids", "live.json", None),
          "wh": ("--warehouses", "wh.json", ASSIGN),
          "prices": ("--prices", "prices.json", PRICES),
          "metrics": ("--overview-metrics", "metrics.json", METRICS),
          "stock": ("--stock-history", "stock.jsonl", None),
          "price": ("--price-history", "price.jsonl", None),
          "hm": ("--hm-last-run", "hm.json", None)}


def _write_inputs(tmp_path, **override):
    now = datetime.now()
    defaults = {"live": _live_doc(),
                "stock": [{"finished_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                           "products_pushed_ok": 4}],
                "price": [{"finished_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                           "pushed_ok": 3}],
                "hm": {"date": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                       "decisions": {"HM-0821677001": {"stock": 2}}}}
    args = []
    for key, (flag, name, data) in INPUTS.items():
        data = override.get(key, defaults.get(key, data))
        p = tmp_path / name
        _write(p, data)
        args += [flag, str(p)]
    return args


def test_main_writes_valid_json(tmp_path, capsys):
    out = tmp_path / "out" / "snap.json"
    out.parent.mkdir()
    assert snap_mod.main(_write_inputs(tmp_path) + ["--out", str(out)]) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["version"] == 2
    assert all(v["status"] == "ok" for v in d["sources"].values()), d["sources"]
    assert d["sources"]["catalog_status"]["built"] == CATALOG["built"]
    assert d["sources"]["live_offer_ids"]["built"] == "2026-11-24T04:10:00"
    assert d["sources"]["warehouse_assignments"]["count"] == len(ASSIGN)
    assert d["sources"]["live_prices"]["count"] == 6  # valid prices in the file
    assert d["sources"]["stock_history"]["lines"] == 1
    assert d["sources"]["catalog_status"]["mtime"]
    assert d["brands"]["M&S"]["stock_up_to_date"] == 4
    assert d["brands"]["M&S"]["price_up_to_date"] == 3
    assert d["brands"]["H&M"]["stock_up_to_date"] == 1
    assert d["campaigns"] == METRICS["campaigns"]
    assert d["warehouses"]["Regular"]["avg_price"] == 75.5
    assert os.listdir(out.parent) == ["snap.json"]
    assert "M&S" in capsys.readouterr().out


def test_main_print_writes_nothing(tmp_path):
    args = _write_inputs(tmp_path)
    out = tmp_path / "snap.json"
    before = sorted(os.listdir(tmp_path))
    assert snap_mod.main(["--print", "--out", str(out)] + args) == 0
    assert not out.exists()
    assert sorted(os.listdir(tmp_path)) == before


SOURCE_KEY = {"cs": "catalog_status", "live": "live_offer_ids", "wh": "warehouse_assignments",
              "prices": "live_prices", "metrics": "overview_metrics", "stock": "stock_history",
              "price": "price_history", "hm": "hm_last_run"}


@pytest.mark.parametrize("which", list(INPUTS))
@pytest.mark.parametrize("kind,status", [(MISSING, "missing"), (b"\xff\xfe garbage {[", "corrupt"),
                                         (["a", "list"], "corrupt")])
def test_main_bad_inputs(tmp_path, capsys, which, kind, status):
    if which in ("stock", "price") and isinstance(kind, list):
        kind = b'"a"\n[1]\n'  # jsonl with no object line
    args = _write_inputs(tmp_path, **{which: kind})
    assert snap_mod.main(["--print"] + args) == 0
    out = tmp_path / "snap.json"
    assert snap_mod.main(["--out", str(out)] + args) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["sources"][SOURCE_KEY[which]]["status"] == status
    if which == "cs":
        assert d["brands"]["M&S"]["offers"] is None and d["total"]["offers"] is None
        assert d["brands"]["M&S"]["stock_up_to_date"] == 4  # capped by live ids instead
    elif which == "live":
        assert d["offers_by_warehouse"] is None and d["warehouses"] is None
        assert d["brands"]["M&S"]["offers"] == 7
        assert d["brands"]["H&M"]["stock_up_to_date"] is None
    elif which == "wh":
        assert d["offers_by_warehouse"]["total"] == {"Unassigned": len(LIVE)}
    elif which == "prices":
        assert all(e["avg_price"] is None and e["price_count"] is None for e in d["warehouses"].values())
    elif which == "metrics":
        assert d["campaigns"] is None and d["sales"] is None and d["metrics_built_at"] is None
    elif which == "stock":
        assert d["brands"]["M&S"]["stock_up_to_date"] is None
    elif which == "price":
        assert d["brands"]["M&S"]["price_up_to_date"] is None
    elif which == "hm":
        assert d["brands"]["H&M"]["stock_up_to_date"] is None
    assert "failed" not in capsys.readouterr().err


def test_main_all_inputs_missing(tmp_path):
    args = _write_inputs(tmp_path, **{k: MISSING for k in INPUTS})
    out = tmp_path / "snap.json"
    assert snap_mod.main(["--out", str(out)] + args) == 0
    d = json.loads(out.read_text(encoding="utf-8"))
    assert all(v["status"] == "missing" for v in d["sources"].values())
    assert d["total"]["offers"] is None and d["warehouses"] is None and d["campaigns"] is None


def test_cli_print_runs_offline_and_writes_nothing(tmp_path):
    before = sorted(os.listdir(ROOT))
    r = subprocess.run([sys.executable, str(ROOT / "account_snapshot.py"), "--print"],
                       cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0, r.stderr
    assert "Account snapshot" in r.stdout and "Wrote" not in r.stdout
    assert sorted(os.listdir(ROOT)) == before
    assert os.listdir(tmp_path) == []


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
