"""Offline tests for planner_ui.pyw's Overview tab data shaping -- no Tk window, no network."""
import ast
import importlib.machinery
import importlib.util
import json
import os
from datetime import datetime, timedelta

import account_snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "planner_ui.pyw")
NOW = datetime(2026, 11, 24, 14, 20, 0)


def _load_app():
    loader = importlib.machinery.SourceFileLoader("planner_ui_overview", APP_PATH)
    spec = importlib.util.spec_from_loader("planner_ui_overview", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ui = _load_app()

CATALOG = {"built": "2026-11-24T14:20:19", "fresh_hours": 26,
           "brands": {"M&S": {"live": 12838, "for_sale": 6243, "stock_fresh": 12114, "price_fresh": 12838},
                      "H&M": {"live": 1273, "for_sale": 310, "stock_fresh": 1270, "price_fresh": 0},
                      "Other": {"live": 0, "for_sale": 0}},
           "total": {"live": 14111, "for_sale": 6553}}

LIVE_IDS = {"built": "2026-11-24T14:20:19", "offer_ids": [
    "MS-T611234-Black-M", "MS-T611234-Black-L", "MS-T611234-Navy-M",
    "MS-T619999-Red-XL", "MS-T619999-Red-M", "MS-123456",
    "HM-1234567001-S", "HM-1234567001-M"]}


def _snapshot():
    sm, reg = account_snapshot.SMALL_WAREHOUSE_ID, account_snapshot.REGULAR_WAREHOUSE_ID
    assignments = {"MS-T611234-Black-M": sm, "MS-T611234-Black-L": sm, "MS-T611234-Navy-M": reg,
                   "MS-T619999-Red-XL": 999, "HM-1234567001-S": sm}
    return account_snapshot.build_snapshot(CATALOG, LIVE_IDS, assignments, {}, now=NOW)


NULL_SNAP = {"version": 1, "built_at": "2026-11-24T10:00:00",
             "brands": {"M&S": {"offers": 5, "for_sale": None, "products": None, "articles": None},
                        "H&M": {}, "Other": {"offers": 0}},
             "total": {"offers": 5, "for_sale": None, "products": None, "articles": None},
             "offers_by_warehouse": None,
             "sources": {"catalog_status": {"status": "ok"}, "live_offer_ids": {"status": "missing"},
                         "warehouse_assignments": {"status": "ok"}}}


# --- KPI / brands -------------------------------------------------------------

def test_kpi_cards_values_and_split():
    cards = ui.kpi_cards(_snapshot())
    assert [c["key"] for c in cards] == ["offers", "for_sale", "products", "articles"]
    assert [c["title"] for c in cards] == ["Total offers", "For sale", "Products", "Articles"]
    offers = cards[0]
    assert offers["value"] == "14,111"
    assert offers["split"] == [("M&S", "12,838"), ("H&M", "1,273"), ("Other", "0")]
    assert offers["sub"] == "M&S 12,838 · H&M 1,273 · Other 0"
    assert cards[1]["value"] == "6,553"
    # snapshot v2 no longer has products/articles: the cards show "—", not a guess
    assert cards[2]["value"] == "—" and cards[2]["split"][0] == ("M&S", "—")
    assert cards[3]["value"] == "—"
    assert cards[2]["note"] and cards[3]["note"]


def test_kpi_cards_nulls_show_dash_not_zero():
    cards = ui.kpi_cards(NULL_SNAP)
    assert cards[0]["value"] == "5"
    assert cards[1]["value"] == "—"
    assert cards[1]["split"] == [("M&S", "—"), ("H&M", "—"), ("Other", "—")]
    assert cards[0]["split"][2] == ("Other", "0")


def test_fmt_num():
    assert ui.fmt_num(12838) == "12,838"
    assert ui.fmt_num(0) == "0"
    for v in (None, True, False, "5", 1.5):
        assert ui.fmt_num(v) == "—"


def test_brand_rows_include_total():
    rows = ui.brand_rows(_snapshot())
    assert [r["brand"] for r in rows] == ["M&S", "H&M", "Other", "Total"]
    ms, total = rows[0], rows[-1]
    # snapshot v2 dropped products/articles/unparsed/sizes -> None in the rows
    assert ms["offers"] == 12838 and ms["unparsed"] is None and ms["sizes_count"] is None
    assert rows[1]["unparsed"] is None and rows[1]["products"] is None
    assert total["offers"] == 14111 and total["unparsed"] is None and total["sizes_count"] is None
    assert ui.brand_rows(NULL_SNAP)[1]["sizes_count"] is None


# --- warehouses -----------------------------------------------------------------

def test_warehouse_rows_order_and_fill():
    columns, rows = ui.warehouse_rows(_snapshot())
    assert columns == ["Warehouse", "M&S", "H&M", "Other", "Total"]
    assert [r[0] for r in rows] == ["Small items", "Regular", "Unassigned", "Other 999"]
    by = {r[0]: r[1:] for r in rows}
    assert by["Small items"] == [2, 1, 0, 3]
    assert by["Regular"] == [1, 0, 0, 1]
    assert by["Unassigned"] == [2, 1, 0, 3]  # MS-T619999-Red-M, MS-123456, HM-...-M
    assert by["Other 999"] == [1, 0, 0, 1]
    assert ui.warehouse_note(_snapshot()) is None


def test_warehouse_rows_unknown():
    columns, rows = ui.warehouse_rows(NULL_SNAP)
    assert rows == [] and columns[0] == "Warehouse"
    assert ui.warehouse_note(NULL_SNAP) == "Unknown: no live offer_id list"


def test_warehouse_rows_fixed_labels_present_when_empty_and_extras_sorted():
    snap = {"offers_by_warehouse": {"total": {"Other 7": 1, "Other 3": 2},
                                    "by_brand": {"M&S": {"Other 7": 1, "Other 3": 2}}}}
    _, rows = ui.warehouse_rows(snap)
    assert [r[0] for r in rows] == ["Small items", "Regular", "Unassigned", "Other 3", "Other 7"]
    assert rows[0] == ["Small items", 0, 0, 0, 0]


# --- sizes -----------------------------------------------------------------------

def test_size_rows_sorting_ties_and_top_n():
    sizes = {"M": 5, "L": 5, "S": 9, "XL": 1, "XS": 2, "bad": "3"}
    rows, hidden = ui.size_rows(sizes, top_n=3)
    assert rows == [("S", 9), ("L", 5), ("M", 5)]
    assert hidden == 2
    rows, hidden = ui.size_rows(sizes, top_n=3, show_all=True)
    assert [r[0] for r in rows] == ["S", "L", "M", "XS", "XL"] and hidden == 0
    rows, hidden = ui.size_rows(sizes, top_n=10)
    assert len(rows) == 5 and hidden == 0


def test_size_rows_bad_input_and_default_top_n():
    assert ui.size_rows(None) == ([], 0)
    assert ui.size_rows(["M"]) == ([], 0)
    sizes = {f"S{i:02d}": i for i in range(20)}
    rows, hidden = ui.size_rows(sizes)
    assert len(rows) == ui.SIZES_TOP_N == 15 and hidden == 5
    assert rows[0] == ("S19", 19)


def test_size_rows_from_snapshot_total():
    # snapshot v2 has no sizes: the size table is empty, not an error
    assert "sizes" not in _snapshot()["total"]
    assert ui.size_rows(_snapshot()["total"].get("sizes")) == ([], 0)


# --- staleness -----------------------------------------------------------------

def test_is_stale_at_26h_boundary():
    built = "2026-11-23T12:00:00"
    assert ui.is_stale(built, datetime(2026, 11, 24, 14, 0, 0)) is False  # exactly 26h: fresh
    assert ui.is_stale(built, datetime(2026, 11, 24, 14, 0, 1)) is True
    assert ui.is_stale(built, datetime(2026, 11, 24, 13, 59, 59)) is False


def test_is_stale_bad_values():
    for v in (None, "", "garbage", 12345, "2026-13-40"):
        assert ui.is_stale(v, NOW) is True


def test_parse_iso_tz_aware_becomes_naive_local():
    t = ui.parse_iso("2026-11-24T10:00:00+00:00")
    assert t is not None and t.tzinfo is None


def test_data_as_of_colour():
    text, color = ui.data_as_of({"built_at": "2026-11-24T14:00:05"}, NOW)
    assert text == "data as of 2026-11-24 14:00" and color is None
    old = (NOW - timedelta(hours=26, seconds=1)).isoformat(timespec="seconds")
    text, color = ui.data_as_of({"built_at": old}, NOW)
    assert color == "warning" and "stale" in text and "26h" in text
    text, color = ui.data_as_of({}, NOW)
    assert color == "warning" and "—" in text


def test_stale_hours_matches_catalog_status():
    with open(os.path.join(ROOT, "catalog_status.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    vals = [n.value.value for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "FRESH_HOURS" for t in n.targets)]
    assert vals == [ui.SNAPSHOT_STALE_HOURS]


# --- catalog freshness / sources ---------------------------------------------------

def test_freshness_rows_from_catalog():
    rows, line, stale = ui.freshness_rows(CATALOG, NOW)
    assert rows == [("M&S", "12,838", "12,838", "12,114"), ("H&M", "1,273", "0", "1,270"),
                    ("Other", "0", "—", "—")]
    assert "2026-11-24 14:20" in line and "26h" in line and stale is False


def test_freshness_rows_missing_and_stale():
    assert ui.freshness_rows(None, NOW) == ([], "catalog_status.json not available", True)
    assert ui.freshness_rows([1], NOW)[1] == "catalog_status.json not available"
    rows, line, stale = ui.freshness_rows({"built": "2026-11-20T00:00:00", "fresh_hours": 12,
                                           "brands": {"H&M": {"live": 3}}}, NOW)
    assert rows == [("H&M", "3", "—", "—")]
    assert stale is True and "12h" in line and "stale" in line


def test_sources_warnings():
    assert ui.sources_warnings(NULL_SNAP) == ["live_offer_ids missing"]
    assert ui.sources_warnings({}) == []


# --- loading -------------------------------------------------------------------

def test_load_snapshot_states(tmp_path):
    p = tmp_path / "account_snapshot.json"
    assert ui.load_snapshot(p) == (None, "missing")
    assert ui.snapshot_placeholder("missing") == "No snapshot yet: click Refresh"

    p.write_text("{not json", encoding="utf-8")
    assert ui.load_snapshot(p) == (None, "corrupt")
    assert "corrupt" in ui.snapshot_placeholder("corrupt") and "Refresh" in ui.snapshot_placeholder("corrupt")

    p.write_text("[1, 2]", encoding="utf-8")
    assert ui.load_snapshot(p) == (None, "corrupt")
    p.write_text(json.dumps({"version": 1, "brands": {}}), encoding="utf-8")
    assert ui.load_snapshot(p) == (None, "corrupt")

    snap = _snapshot()
    p.write_text(json.dumps(snap), encoding="utf-8")
    data, state = ui.load_snapshot(p)
    assert state == "ok" and data["total"]["offers"] == 14111
    assert ui.snapshot_placeholder("ok") is None

    p.write_text(json.dumps(dict(snap, version=2)), encoding="utf-8")
    assert ui.load_snapshot(p)[1] == "ok"


def test_snapshot_written_by_script_loads(tmp_path):
    cs, li, wh = tmp_path / "cs.json", tmp_path / "li.json", tmp_path / "wh.json"
    cs.write_text(json.dumps(CATALOG), encoding="utf-8")
    li.write_text(json.dumps(LIVE_IDS), encoding="utf-8")
    wh.write_text("{}", encoding="utf-8")
    out = tmp_path / "account_snapshot.json"
    assert account_snapshot.main(["--out", str(out), "--catalog-status", str(cs),
                                  "--live-ids", str(li), "--warehouses", str(wh)]) == 0
    snap, state = ui.load_snapshot(out)
    assert state == "ok"
    assert ui.kpi_cards(snap)[0]["value"] == "14,111"
    _, rows = ui.warehouse_rows(snap)
    assert [r[0] for r in rows] == ["Small items", "Regular", "Unassigned"]
    assert rows[2][-1] == len(LIVE_IDS["offer_ids"])


# --- commands stay read-only --------------------------------------------------------

READ_ONLY_SCRIPTS = {"catalog_status.py", "category_inventory.py", "account_snapshot.py"}


def test_account_snapshot_command():
    assert ui.COMMANDS["account_snapshot"] == ["account_snapshot.py"]
    assert ui.TIMEOUTS["account_snapshot"] > 0
    assert set(ui.TIMEOUTS) == set(ui.COMMANDS)


def test_every_command_is_read_only():
    for action, argv in ui.COMMANDS.items():
        script = argv[0]
        if script == "upload_to_ozon.py":
            assert "--dry-run" in argv
        else:
            assert script in READ_ONLY_SCRIPTS, action
        assert not any("--apply" in a for a in argv)


def test_run_script_account_snapshot_not_found(tmp_path):
    rc, out = ui.run_script("account_snapshot", cwd=tmp_path)
    assert rc is None and "not found" in out


def test_overview_tab_registered_first():
    assert ui.tab_names()[0] == "Overview"
    assert ui.TAB_BUILDERS["Overview"] is ui.OverviewTab
    assert ui.DEFAULT_TAB == "Prioritization"
