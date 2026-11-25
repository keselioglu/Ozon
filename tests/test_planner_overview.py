"""Offline tests for planner_ui.pyw's Overview tab data shaping (snapshot v2) -- no Tk window, no network."""
import ast
import importlib.machinery
import importlib.util
import json
import os
from datetime import datetime, timedelta

import pytest

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
           "brands": {"M&S": {"live": 12838, "for_sale": 6243},
                      "H&M": {"live": 1273, "for_sale": 310},
                      "Other": {"live": 0, "for_sale": 0}},
           "total": {"live": 14111, "for_sale": 6553}}

LIVE_IDS = {"built": "2026-11-24T14:20:19", "offer_ids": [
    "MS-T611234-Black-M", "MS-T611234-Black-L", "MS-T611234-Navy-M",
    "MS-T619999-Red-XL", "MS-T619999-Red-M", "MS-123456",
    "HM-1234567001-S", "HM-1234567001-M"]}

PRICES = {"built": "2026-11-24T14:20:19", "prices": {
    "MS-T611234-Black-M": 20.0, "MS-T611234-Black-L": 30.5, "MS-T611234-Navy-M": 75.0,
    "MS-T619999-Red-XL": 61.0, "HM-1234567001-S": 10.0}}

METRICS = {"version": 1, "built_at": "2026-11-24T10:00:05+03:00",
           "campaigns": {"live_skus": 812, "ratio_count": 640, "avg_ratio_pct": 38.24,
                         "min_ratio_pct": 21.0, "max_ratio_pct": 46.0},
           "sales": {"window_from": "2026-11-23T00:00:00+03:00", "window_to": "2026-11-24T10:00:00+03:00",
                     "revenue_usd": 1234.5, "orders": 17, "units": 21, "skus": 19,
                     "avg_ms_cost_pct": 41.3,
                     "warehouse_pct": {"small": 66.7, "regular": 33.3, "unknown": 0.0},
                     "brand_pct": {"M&S": 90.5, "H&M": 9.5, "Other": 0.0},
                     "top_category": {"label": "Pajama", "units": 6}, "non_usd_skipped": 0}}

STOCK_HISTORY = [{"finished_at": "2026-11-24T05:10:00", "products_pushed_ok": 12000}]
PRICE_HISTORY = [{"finished_at": "2026-11-24T06:00:00", "pushed_ok": 6419}]


def _snapshot(metrics=METRICS):
    sm, reg = account_snapshot.SMALL_WAREHOUSE_ID, account_snapshot.REGULAR_WAREHOUSE_ID
    assignments = {"MS-T611234-Black-M": sm, "MS-T611234-Black-L": sm, "MS-T611234-Navy-M": reg,
                   "MS-T619999-Red-XL": 999, "HM-1234567001-S": sm}
    return account_snapshot.build_snapshot(CATALOG, LIVE_IDS, assignments, {}, now=NOW,
                                           prices=PRICES, overview_metrics=metrics,
                                           stock_history=STOCK_HISTORY, price_history=PRICE_HISTORY)


NULL_SNAP = {"version": 2, "built_at": "2026-11-24T10:00:00",
             "brands": {"M&S": {"offers": 5, "for_sale": None}, "H&M": {}, "Other": {"offers": 0}},
             "total": {"offers": 5, "for_sale": None, "stock_up_to_date": None, "price_up_to_date": None},
             "warehouses": None, "offers_by_warehouse": None,
             "campaigns": None, "sales": None, "metrics_built_at": None,
             "sources": {"catalog_status": {"status": "ok"}, "live_offer_ids": {"status": "missing"},
                         "warehouse_assignments": {"status": "ok"}}}

GARBAGE = (None, [], "x", 5, {"campaigns": [1], "sales": "x", "metrics_built_at": 12},
           {"metrics_built_at": "garbage", "campaigns": {}, "sales": {}},
           {"metrics_built_at": "2026-11-24T10:00:00", "campaigns": "nope", "sales": [1, 2]},
           {"metrics_built_at": "2026-11-24T10:00:00",
            "campaigns": {"live_skus": "x", "avg_ratio_pct": None, "min_ratio_pct": True, "ratio_count": 1.5},
            "sales": {"window_from": 3, "window_to": None, "revenue_usd": "1", "orders": None,
                      "units": "u", "skus": None, "warehouse_pct": [1], "brand_pct": "x",
                      "top_category": ["Pajama"], "avg_ms_cost_pct": float("nan")}})


# --- KPI cards -------------------------------------------------------------------

def test_kpi_cards_keys_and_titles():
    cards = ui.kpi_cards(_snapshot())
    assert [c["key"] for c in cards] == ["offers", "for_sale", "stock_up_to_date", "price_up_to_date"]
    assert [c["title"] for c in cards] == ["Total SKUs", "For sale", "Stock up to date (24h)",
                                           "Price up to date (24h)"]


def test_kpi_cards_values_split_and_share():
    cards = {c["key"]: c for c in ui.kpi_cards(_snapshot())}
    total = cards["offers"]
    assert total["value"] == "14,111"
    assert total["split"] == [("M&S", "12,838"), ("H&M", "1,273"), ("Other", "0")]
    assert total["sub"] == "M&S 12,838 · H&M 1,273 · Other 0"
    assert total["share"] == "100.0%" and total["share_text"] == "100.0% of total SKUs"
    assert cards["for_sale"]["value"] == "6,553" and cards["for_sale"]["share"] == "46.4%"
    stock = cards["stock_up_to_date"]
    assert stock["split"][0] == ("M&S", "12,000") and stock["split"][2] == ("Other", "0")
    price = cards["price_up_to_date"]
    assert price["value"] == "6,419" and price["split"][1] == ("H&M", "0")
    assert price["share"] == "45.5%"
    assert "H&M: no price run" in price["note"] and "Other: no price run" in price["note"]
    assert cards["offers"]["note"] is None


def test_kpi_cards_nulls_show_dash_not_zero():
    cards = ui.kpi_cards(NULL_SNAP)
    assert cards[0]["value"] == "5" and cards[0]["share"] == "100.0%"
    assert cards[1]["value"] == "—" and cards[1]["share"] == "—"
    assert cards[1]["split"] == [("M&S", "—"), ("H&M", "—"), ("Other", "—")]
    assert cards[0]["split"][2] == ("Other", "0")
    for bad in (None, [], "x", {"total": [1], "brands": 3}):
        cards = ui.kpi_cards(bad)
        assert len(cards) == 4 and all(c["value"] == "—" and c["share"] == "—" for c in cards)


def test_share_pct_and_formatters():
    assert ui.share_pct(1, 3) == "33.3%"
    assert ui.share_pct(0, 0) == "—" and ui.share_pct(None, 5) == "—" and ui.share_pct(5, True) == "—"
    assert ui.fmt_num(12838) == "12,838" and ui.fmt_num(0) == "0"
    for v in (None, True, False, "5", 1.5):
        assert ui.fmt_num(v) == "—"
    assert ui.fmt_usd(1234.5) == "$1,234.50" and ui.fmt_usd(0) == "$0.00"
    for v in (None, True, "1", float("nan"), float("inf")):
        assert ui.fmt_usd(v) == "—" and ui.fmt_pct(v) == "—"
    assert ui.fmt_pct(38.24) == "38.2%"


# --- brands ---------------------------------------------------------------------

def test_brand_rows_only_brand_skus_for_sale():
    rows = ui.brand_rows(_snapshot())
    assert [r["brand"] for r in rows] == ["M&S", "H&M", "Other", "Total"]
    assert all(set(r) == {"brand", "skus", "for_sale"} for r in rows)
    assert rows[0] == {"brand": "M&S", "skus": 12838, "for_sale": 6243}
    assert rows[-1] == {"brand": "Total", "skus": 14111, "for_sale": 6553}
    assert ui.brand_rows(NULL_SNAP)[1] == {"brand": "H&M", "skus": None, "for_sale": None}
    assert [k for k, _, _ in ui.BRAND_TABLE_COLUMNS] == ["brand", "skus", "for_sale"]
    assert len(ui.brand_rows(None)) == 4


# --- warehouses -----------------------------------------------------------------

def test_warehouse_rows_prices_formatted():
    columns, rows = ui.warehouse_rows(_snapshot())
    assert columns == ["Warehouse", "Live SKUs", "Avg price", "Min price", "Max price"]
    assert [r[0] for r in rows] == ["Small items", "Regular", "Unassigned", "Other 999"]
    by = {r[0]: r[1:] for r in rows}
    assert by["Small items"] == ["3", "$20.17", "$10.00", "$30.50"]
    assert by["Regular"] == ["1", "$75.00", "$75.00", "$75.00"]
    assert by["Unassigned"] == ["3", "—", "—", "—"]  # no prices for those SKUs
    assert by["Other 999"] == ["1", "$61.00", "$61.00", "$61.00"]
    assert ui.warehouse_note(_snapshot()) is None


def test_warehouse_rows_null_prices_and_unknown():
    snap = {"warehouses": {"Regular": {"skus": 4, "avg_price": None, "min_price": None, "max_price": None},
                           "Small items": {"skus": 2, "avg_price": 9.5, "min_price": "x", "max_price": 12}}}
    _, rows = ui.warehouse_rows(snap)
    assert rows == [["Small items", "2", "$9.50", "—", "$12.00"], ["Regular", "4", "—", "—", "—"]]
    columns, rows = ui.warehouse_rows(NULL_SNAP)
    assert rows == [] and columns[0] == "Warehouse"
    assert ui.warehouse_note(NULL_SNAP) == "Unknown: no live offer_id list"
    for bad in (None, [], {"warehouses": [1]}):
        assert ui.warehouse_rows(bad)[1] == []
    assert ui.warehouse_rows({"warehouses": {"Regular": "bad"}})[1] == [["Regular", "—", "—", "—", "—"]]


# --- campaigns / sales cards ----------------------------------------------------------

def test_campaign_card_values():
    card = ui.campaign_card(_snapshot(), NOW)
    assert card["title"] == "Campaigns" and card["message"] is None and card["stale"] is False
    assert card["lines"] == [("Live SKUs in campaigns", "812"), ("Avg cost/sale", "38.2%"),
                             ("Min cost/sale", "21.0%")]
    assert "640" in card["caption"]
    assert card["status_color"] is None


def test_sales_card_values():
    card = ui.sales_card(_snapshot(), NOW)
    assert card["title"] == "Sales last 34 hours" and card["message"] is None
    assert card["caption"].startswith("updated daily 10:00 · ") and " → " in card["caption"]
    lines = dict(card["lines"])
    assert lines["Revenue"] == "$1,234.50"
    assert lines["Orders"] == "17"
    assert lines["SKUs"] == "19 (21 units)"
    assert lines["Avg cost/sale"] == "41.3%"
    assert lines["Small / regular"] == "66.7% / 33.3%"
    assert lines["Brand sale %"] == "M&S 90.5% · H&M 9.5% · Other 0.0%"
    assert lines["Top sold category"] == "Pajama (6 units)"
    assert card["status"] is None  # fresh: the caption already dates the window


def test_sales_card_unknown_warehouse_share_shown():
    m = json.loads(json.dumps(METRICS))
    m["sales"]["warehouse_pct"] = {"small": 50.0, "regular": 25.0, "unknown": 25.0}
    m["sales"]["top_category"] = None
    lines = dict(ui.sales_card(_snapshot(m), NOW)["lines"])
    assert lines["Small / regular"] == "50.0% / 25.0% (unknown 25.0%)"
    assert lines["Top sold category"] == "—"


def test_metrics_null_shows_no_data_text():
    for snap in (NULL_SNAP, _snapshot(metrics=None)):
        for card in (ui.campaign_card(snap, NOW), ui.sales_card(snap, NOW)):
            assert card["message"] == "No sales/campaign data yet: the 10:00 daily report writes it"
            assert card["lines"] == [] and card["stale"] is False
    assert ui.sales_card(NULL_SNAP, NOW)["caption"] == "updated daily 10:00 · — → —"


def test_metrics_stale_flag():
    fresh = dict(_snapshot(), metrics_built_at="2026-11-23T12:20:00")  # exactly 26h
    stale = dict(_snapshot(), metrics_built_at="2026-11-23T12:19:59")
    for fn in (ui.campaign_card, ui.sales_card):
        assert fn(fresh, NOW)["stale"] is False and fn(fresh, NOW)["status_color"] is None
        card = fn(stale, NOW)
        assert card["stale"] is True and card["status_color"] == "warning"
        assert "stale" in card["status"] and "26h" in card["status"]
        assert card["lines"]  # stale data is still shown, just coloured


def test_metrics_cards_never_raise_on_garbage():
    for bad in GARBAGE:
        for fn in (ui.campaign_card, ui.sales_card):
            card = fn(bad, NOW)
            assert isinstance(card, dict) and card["title"]
            for _, value in card["lines"]:
                assert isinstance(value, str)
            card = fn(bad, None)  # no `now`
            assert isinstance(card, dict)
    card = ui.sales_card(GARBAGE[-1], NOW)
    lines = dict(card["lines"])
    assert lines["Revenue"] == "—" and lines["SKUs"] == "—" and lines["Top sold category"] == "—"
    assert lines["Avg cost/sale"] == "—"
    assert dict(ui.campaign_card(GARBAGE[-1], NOW)["lines"])["Live SKUs in campaigns"] == "—"
    msg = ui.campaign_card({"metrics_built_at": "2026-11-24T10:00:00", "campaigns": None}, NOW)["message"]
    assert msg and "campaign" in msg


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


def test_sources_warnings():
    assert ui.sources_warnings(NULL_SNAP) == ["live_offer_ids missing"]
    assert ui.sources_warnings({}) == []


# --- removed sections stay removed ---------------------------------------------------

def test_sizes_and_catalog_freshness_are_gone():
    with open(APP_PATH, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    names |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    for gone in ("size_rows", "freshness_rows", "SIZES_TOP_N", "on_toggle_sizes",
                 "_render_sizes", "_render_freshness", "CATALOG_MISSING_NOTE"):
        assert gone not in names, gone
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("Catalog freshness" in s for s in strings)
    assert not any("Sizes" in s for s in strings)
    for word in ("Products", "Articles", "Unparsed"):
        assert not any(s == word for s in strings), word


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
    p.write_text(json.dumps({"version": 2, "brands": {}}), encoding="utf-8")
    assert ui.load_snapshot(p) == (None, "corrupt")

    snap = _snapshot()
    p.write_text(json.dumps(snap), encoding="utf-8")
    data, state = ui.load_snapshot(p)
    assert state == "ok" and data["total"]["offers"] == 14111 and data["version"] == 2
    assert ui.snapshot_placeholder("ok") is None


def test_snapshot_written_by_script_loads(tmp_path):
    files = {"cs": CATALOG, "li": LIVE_IDS, "wh": {}, "pr": PRICES, "om": METRICS,
             "hm": {"date": "2026-11-20", "decisions": {}}}
    for name, data in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "sh.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "ph.jsonl").write_text("", encoding="utf-8")
    out = tmp_path / "account_snapshot.json"
    t = str(tmp_path)
    assert account_snapshot.main(["--out", str(out), "--catalog-status", f"{t}/cs.json",
                                  "--live-ids", f"{t}/li.json", "--warehouses", f"{t}/wh.json",
                                  "--prices", f"{t}/pr.json", "--overview-metrics", f"{t}/om.json",
                                  "--stock-history", f"{t}/sh.jsonl", "--price-history", f"{t}/ph.jsonl",
                                  "--hm-last-run", f"{t}/hm.json"]) == 0
    snap, state = ui.load_snapshot(out)
    assert state == "ok"
    assert ui.kpi_cards(snap)[0]["value"] == "14,111"
    _, rows = ui.warehouse_rows(snap)
    assert [r[0] for r in rows] == ["Unassigned"]
    assert rows[0][1] == str(len(LIVE_IDS["offer_ids"]))
    assert dict(ui.campaign_card(snap, NOW)["lines"])["Live SKUs in campaigns"] == "812"
    assert dict(ui.sales_card(snap, NOW)["lines"])["Orders"] == "17"


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
    assert ui.DEFAULT_TAB == "New Product Adding"


# --- optional real-Tk smoke test (skipped without a display) ---------------------

def test_overview_renders_in_real_tk(tmp_path, monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from tkinter import ttk
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("no display")
    try:
        root.withdraw()

        class App:
            def __init__(self):
                self.root, self.tk, self.ttk, self.dpi = root, tkinter, ttk, 1.0
                self.posted = []

            def post(self, fn, *args):
                self.posted.append((fn, args))

        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)

        monkeypatch.setattr(ui, "HERE", tmp_path)
        frame = tkinter.Frame(root, bg=ui.BG)
        tab = ui.OverviewTab(App(), frame)  # no snapshot yet: placeholder only
        texts = [w.cget("text") for w in walk(frame) if isinstance(w, tkinter.Label)]
        assert ui.NO_SNAPSHOT_TEXT in texts

        for snap in (_snapshot(), NULL_SNAP):
            (tmp_path / ui.ACCOUNT_SNAPSHOT_FILE).write_text(json.dumps(snap), encoding="utf-8")
            tab.load_local()
            texts = [w.cget("text") for w in walk(frame) if isinstance(w, tkinter.Label)]
            for title in ("Total SKUs", "Brands", "Campaigns", "Sales last 34 hours"):
                assert title in texts
            assert not any("Sizes" in t or "Catalog freshness" in t for t in texts)
        assert ui.NO_METRICS_TEXT in texts
        buttons = [w for w in walk(frame) if isinstance(w, tkinter.Button)]
        assert [b.cget("text") for b in buttons] == ["Refresh"]
    finally:
        root.destroy()
