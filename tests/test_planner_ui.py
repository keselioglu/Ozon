"""Offline tests for planner_ui.pyw's data/plan logic -- no Tk window, no network."""
import ast
import importlib.machinery
import importlib.util
import os
import re

import pytest

import daily_plan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "planner_ui.pyw")
TODAY = "2026-11-24"


def _load_app():
    loader = importlib.machinery.SourceFileLoader("planner_ui", APP_PATH)
    spec = importlib.util.spec_from_loader("planner_ui", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ui = _load_app()


def _inventory():
    cats = {lbl: {"live_offers": 0, "crawled_not_live_offers": 0, "deferred_offers": 0,
                  "discovered_not_live_offers_est": 0} for lbl in daily_plan.CATEGORY_LABELS.values()}
    cats["Pajama"].update(live_offers=100, crawled_not_live_offers=40, deferred_offers=10,
                          discovered_not_live_offers_est=50)
    cats["Socks"].update(live_offers=20, crawled_not_live_offers=5, deferred_offers=0,
                         discovered_not_live_offers_est=5)
    return {
        "built": "2026-11-24T05:00:00",
        "brands": {"M&S": {"categories": cats}, "H&M": {"live": 1200, "add_pipeline": False}},
        "created_history": [
            {"date": "2026-11-17", "created_total": 99, "by_category": {"Pajama": 99}},  # outside 7d
            {"date": "2026-11-20", "created_total": 30, "by_category": {"Pajama": 20, "Socks": 10}},
            {"date": TODAY, "created_total": 7, "by_category": {"Pajama": 7}},
        ],
    }


CATALOG = {"built": "2026-11-24T14:20:19",
           "brands": {"M&S": {"live": 12838, "for_sale": 6243, "stock_fresh": 12114, "price_fresh": 12838},
                      "H&M": {"live": 1273, "for_sale": 310, "stock_fresh": 1270, "price_fresh": 0},
                      "Other": {"live": 0}}}


def test_file_parses():
    with open(APP_PATH, encoding="utf-8") as f:
        ast.parse(f.read())


def test_import_does_not_open_tk():
    assert "tkinter" not in ui.__dict__  # Tk is only imported inside PlannerApp


# --- build_rows ---------------------------------------------------------------

def test_build_rows_numbers_and_days_of_supply():
    created = ui.created_from_history(_inventory(), TODAY)
    day = {"total": 500, "allocations": {"M&S": {"Pajama": 20}}, "fill_remainder": True}
    rows = ui.build_rows(_inventory(), CATALOG, day, created)
    by = {(r["brand"], r["category"]): r for r in rows}
    assert len(rows) == len(daily_plan.CATEGORY_LABELS) + 1
    pj = by[("M&S", "Pajama")]
    assert (pj["live"], pj["crawled_not_live"], pj["deferred"], pj["discovered_est"]) == (100, 40, 10, 50)
    assert pj["created_today"] == 7
    assert pj["created_7d"] == 27  # 20 + 7, the 2026-11-17 entry is outside the window
    assert pj["allocation"] == 20
    assert pj["pool"] == 100
    assert pj["days_of_supply"] == 5.0
    socks = by[("M&S", "Socks")]
    assert socks["allocation"] == 0 and socks["days_of_supply"] is None
    assert socks["created_7d"] == 10


def test_build_rows_hm_read_only():
    rows = ui.build_rows(_inventory(), CATALOG, daily_plan.plan_for_date(daily_plan.default_plan(), TODAY),
                         {})
    hm = [r for r in rows if r["brand"] == "H&M"]
    assert len(hm) == 1
    assert hm[0]["live"] == 1273
    assert hm[0]["note"] == "no add pipeline yet"
    assert not ui.is_editable(hm[0])
    assert ui.fmt_cell("allocation", hm[0]) == "no add pipeline yet"
    assert all(ui.is_editable(r) for r in rows if r["brand"] == "M&S")


def test_build_rows_with_no_cached_files():
    rows = ui.build_rows(None, None, daily_plan.plan_for_date(daily_plan.default_plan(), TODAY), None)
    assert all(r["live"] in (0, None) for r in rows)


def test_merge_created_today_uses_fresh_split():
    created = ui.created_from_history(_inventory(), TODAY)
    merged = ui.merge_created_today(created, {("M&S", "Pajama"): 12, ("M&S", "Dress"): 3, ("H&M", "Top"): 9})
    assert merged["today"] == {"Pajama": 12, "Dress": 3}
    assert merged["7d"]["Pajama"] == 32
    assert merged["7d"]["Dress"] == 3


def test_created_today_total_and_deferred():
    assert ui.created_today_total({"date": TODAY, "new_offer_ids": ["a", "b"]}, TODAY) == 2
    assert ui.created_today_total({"date": "2026-11-23", "new_offer_ids": ["a"]}, TODAY) == 0
    assert ui.created_today_total(None, TODAY) == 0
    assert ui.deferred_count([{}, {}]) == 2
    assert ui.deferred_count(None) == 0


def test_quota_text_unknown_when_missing():
    assert ui.quota_text(None)[0] == "unknown"
    assert ui.quota_text({"limit": None, "usage": 3})[0] == "unknown"
    assert ui.quota_text({"limit": 2000, "usage": 120, "reset_at": "x"})[0] == "120 / 2,000"


def test_brand_cards_skip_empty_brands():
    cards = ui.brand_cards(CATALOG)
    assert [c[0] for c in cards] == ["M&S", "H&M"]
    assert "6,243 for sale" in cards[0][2]


# --- plan editing -------------------------------------------------------------

def test_day_keys_cover_default_and_next_7_days():
    keys = ui.day_keys(TODAY)
    assert keys[0] == "Default"
    assert keys[1] == TODAY
    assert keys[-1] == "2026-12-01"
    assert len(keys) == 1 + 1 + 7
    assert ui.day_label("2026-11-25", TODAY).startswith("Today+1")


def test_get_day_without_override_copies_default():
    plan = daily_plan.default_plan()
    plan["default"]["allocations"] = {"M&S": {"Pajama": 5}}
    day = ui.get_day(plan, "2026-11-26")
    day["allocations"]["M&S"]["Pajama"] = 99
    assert plan["default"]["allocations"]["M&S"]["Pajama"] == 5
    assert not ui.has_override(plan, "2026-11-26")


def test_set_allocation_refuses_hm():
    day = ui.get_day(daily_plan.default_plan(), "Default")
    with pytest.raises(ValueError):
        ui.set_allocation(day, "H&M", "Top", 10)
    with pytest.raises(ValueError):
        ui.set_allocation(day, "M&S", "Top", -1)
    assert ui.set_allocation(day, "M&S", "Top", 10)["allocations"] == {"M&S": {"Top": 10}}
    assert day["allocations"] == {}


def test_spread_evenly_and_by_pool():
    assert ui.spread_evenly(10, ["a", "b", "c"]) == {"a": 4, "b": 3, "c": 3}
    assert sum(ui.spread_evenly(500, list(daily_plan.CATEGORY_LABELS.values())).values()) == 500
    assert ui.by_pool_size(100, {"a": 300, "b": 100, "c": 0}) == {"a": 75, "b": 25, "c": 0}
    assert sum(ui.by_pool_size(7, {"a": 1, "b": 1, "c": 1, "d": 10}).values()) == 7
    assert ui.by_pool_size(500, {"a": 30, "b": 20}) == {"a": 30, "b": 20}  # never above the pool
    assert ui.by_pool_size(500, {"a": 0}) == {"a": 0}


def test_save_roundtrip_default_and_override(tmp_path):
    path = str(tmp_path / "daily_plan.json")
    plan = daily_plan.default_plan()
    day = ui.set_allocation(ui.get_day(plan, "Default"), "M&S", "Pajama", 120)
    day = ui.set_allocation(day, "M&S", "Socks", 0)  # zero entries are dropped
    plan = ui.set_day(plan, "Default", day)
    over = ui.get_day(plan, "2026-11-26")
    over["total"] = 300
    over["fill_remainder"] = False
    plan = ui.set_day(plan, "2026-11-26", ui.apply_helper(over, {"Dress": 50, "Top": 0}))
    saved, errors = ui.save_plan_from_ui(plan, path)
    assert errors == []
    loaded = daily_plan.load_plan(path)
    assert loaded == saved
    assert loaded["default"] == {"total": 500, "allocations": {"M&S": {"Pajama": 120}}, "fill_remainder": True}
    assert daily_plan.plan_for_date(loaded, "2026-11-26") == \
        {"total": 300, "allocations": {"M&S": {"Dress": 50}}, "fill_remainder": False}
    assert ui.get_day(loaded, "2026-11-26") == daily_plan.plan_for_date(loaded, "2026-11-26")

    cleared, errors = ui.save_plan_from_ui(ui.clear_override(loaded, "2026-11-26"), path)
    assert errors == [] and daily_plan.load_plan(path)["overrides"] == {}
    assert cleared["default"] == loaded["default"]


def test_over_total_cannot_be_saved(tmp_path):
    path = tmp_path / "daily_plan.json"
    day = ui.get_day(daily_plan.default_plan(), "Default")
    day["total"] = 100
    day = ui.apply_helper(day, {"Pajama": 60, "Socks": 41})
    assert ui.allocation_sum(day) == 101
    plan = ui.set_day(daily_plan.default_plan(), "Default", day)
    saved, errors = ui.save_plan_from_ui(plan, str(path))
    assert saved is None and errors
    assert not path.exists()


# --- safety: read-only toward Ozon --------------------------------------------

def test_ozon_read_refuses_other_endpoints():
    for path in ("/v3/product/import", "/v1/actions/products/activate", "/v2/products/stocks"):
        with pytest.raises(PermissionError):
            ui.ozon_read(path)


def test_source_names_only_the_quota_endpoint():
    with open(APP_PATH, encoding="utf-8") as f:
        src = f.read()
    endpoints = set(re.findall(r"[\"'](/v\d+/[a-z0-9/_\-]+)[\"']", src))
    assert endpoints == {"/v4/product/info/limit"}
    assert "call(" not in src.replace("return call(path", "")  # only ozon_read calls ozon_client


def test_commands_never_upload_for_real():
    for action, argv in ui.COMMANDS.items():
        if "upload_to_ozon.py" in argv:
            assert "--dry-run" in argv
        assert not any("--apply" in a for a in argv)
    assert ui.command_argv("preview")[-2:] == ["upload_to_ozon.py", "--dry-run"]


def test_read_quota_offline_returns_none(monkeypatch):
    import ozon_client
    monkeypatch.setattr(ozon_client, "call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert ui.read_quota() is None


def test_run_script_missing_file_is_skipped(tmp_path):
    rc, out = ui.run_script("catalog_status", cwd=tmp_path)
    assert rc is None and "not found" in out


# --- shortcut installer -------------------------------------------------------

def test_shortcut_ps_command_quotes_paths():
    import install_desktop_shortcut as inst
    cmd = inst.build_ps_command(r"C:\Users\O'Neil\Desktop\Ozon Daily Planner.lnk",
                                r"C:\Py\pythonw.exe", r"C:\repo\planner_ui.pyw", r"C:\repo")
    assert "O''Neil" in cmd
    assert "$s.Arguments = '\"C:\\repo\\planner_ui.pyw\"'" in cmd
    assert "$s.WorkingDirectory = 'C:\\repo'" in cmd
    assert inst.shortcut_path().name == "Ozon Sales.lnk"


class _FakeRun:
    """Stands in for subprocess.run: records argv, optionally writes the .lnk."""

    def __init__(self, lnk, returncode=0):
        self.lnk, self.returncode, self.calls = lnk, returncode, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.returncode == 0:
            self.lnk.write_bytes(b"lnk")
        return type("P", (), {"returncode": self.returncode, "stdout": "", "stderr": ""})()


def _shortcut_env(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    legacy = desktop / "Ozon Daily Planner.lnk"
    legacy.write_bytes(b"old")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "planner_ui.pyw").write_text("", encoding="utf-8")
    return desktop, legacy, repo


def test_shortcut_main_creates_new_and_removes_legacy_idempotent(monkeypatch, tmp_path):
    import install_desktop_shortcut as inst
    desktop, legacy, repo = _shortcut_env(monkeypatch, tmp_path)
    fake = _FakeRun(desktop / "Ozon Sales.lnk")
    monkeypatch.setattr(inst.subprocess, "run", fake)
    assert inst.main(["--repo", str(repo)]) == 0
    assert (desktop / "Ozon Sales.lnk").exists()
    assert not legacy.exists()
    cmd = " ".join(fake.calls[0])
    assert "Ozon Sales.lnk" in cmd and "planner_ui.pyw" in cmd
    # (b) second run: nothing left to remove, same shortcut rewritten
    assert inst.main(["--repo", str(repo)]) == 0
    assert len(fake.calls) == 2
    assert (desktop / "Ozon Sales.lnk").exists()


def test_shortcut_main_failure_keeps_legacy(monkeypatch, tmp_path):
    import install_desktop_shortcut as inst
    desktop, legacy, repo = _shortcut_env(monkeypatch, tmp_path)
    fake = _FakeRun(desktop / "Ozon Sales.lnk", returncode=1)
    monkeypatch.setattr(inst.subprocess, "run", fake)
    assert inst.main(["--repo", str(repo)]) == 1
    assert legacy.exists()
    assert not (desktop / "Ozon Sales.lnk").exists()


def test_shortcut_legacy_removal_error_still_succeeds(monkeypatch, tmp_path):
    import install_desktop_shortcut as inst
    desktop, legacy, repo = _shortcut_env(monkeypatch, tmp_path)
    monkeypatch.setattr(inst.subprocess, "run", _FakeRun(desktop / "Ozon Sales.lnk"))

    def boom(self, *a, **k):
        raise OSError("locked")
    monkeypatch.setattr(inst.Path, "unlink", boom)
    assert inst.main(["--repo", str(repo)]) == 0
    assert legacy.exists()


def test_shortcut_names(monkeypatch, tmp_path):
    import install_desktop_shortcut as inst
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert inst.shortcut_path().name == "Ozon Sales.lnk"
    assert inst.shortcut_path().parent == tmp_path / "Desktop"
    assert [p.name for p in inst.legacy_shortcut_paths()] == ["Ozon Daily Planner.lnk"]


# --- tabbed shell -------------------------------------------------------------

def test_app_title_and_tabs():
    assert ui.APP_TITLE == "Ozon Sales"
    assert ui.TAB_NAMES == ("Overview", "Daily tasks", "New Product Adding")
    assert ui.tab_names() == list(ui.TAB_NAMES)
    assert ui.DEFAULT_TAB == "New Product Adding"
    assert ui.NewProductAddingTab is ui.PrioritizationTab
    assert ui.PlannerApp is ui.OzonSalesApp


def test_source_assigns_title_and_defines_prioritization_tab():
    with open(APP_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    titles = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "APP_TITLE" for t in n.targets)]
    assert titles and isinstance(titles[0].value, ast.Constant) and titles[0].value.value == "Ozon Sales"
    assert any(isinstance(n, ast.ClassDef) and n.name == "PrioritizationTab" for n in ast.walk(tree))


def test_register_tab_appends_replaces_in_place_and_decorates(monkeypatch):
    monkeypatch.setattr(ui, "TAB_BUILDERS", dict(ui.TAB_BUILDERS))

    def dummy(app, parent):
        return "dummy"

    assert ui.register_tab("Extra", dummy) is dummy
    assert ui.tab_names() == list(ui.TAB_NAMES) + ["Extra"]

    def other(app, parent):
        return "other"

    ui.register_tab("Overview", other)
    assert ui.tab_names() == list(ui.TAB_NAMES) + ["Extra"]
    assert ui.TAB_BUILDERS["Overview"] is other

    @ui.register_tab("Decorated")
    def decorated(app, parent):
        return "decorated"

    assert ui.TAB_BUILDERS["Decorated"] is decorated
    assert decorated(None, None) == "decorated"
    assert ui.tab_names()[-1] == "Decorated"


def test_register_tab_changes_are_restored():
    assert ui.tab_names() == list(ui.TAB_NAMES)
    assert ui.TAB_BUILDERS["New Product Adding"] is ui.PrioritizationTab


def test_read_only_endpoints_and_commands_unchanged():
    assert ui.READ_ONLY_ENDPOINTS == frozenset({"/v4/product/info/limit"})
    assert set(ui.COMMANDS) == {"catalog_status", "inventory", "walk", "preview", "account_snapshot"}
    assert ui.COMMANDS["catalog_status"] == ["catalog_status.py"]
    assert ui.COMMANDS["inventory"] == ["category_inventory.py"]
    assert ui.COMMANDS["walk"] == ["category_inventory.py", "--walk"]
    assert ui.COMMANDS["preview"] == ["upload_to_ozon.py", "--dry-run"]
    assert ui.COMMANDS["account_snapshot"] == ["account_snapshot.py"]


# --- New Product Adding table -------------------------------------------------

def test_source_uses_new_product_plan_and_new_sources_without_new_endpoints():
    with open(APP_PATH, encoding="utf-8") as f:
        src = f.read()
    assert "import new_product_plan" in src and "import new_sources" in src
    assert "new_product_plan.build_rows(" in src
    assert "new_product_plan.save_plan(" in src
    assert "new_sources.create_request(" in src
    endpoints = set(re.findall(r"[\"'](/v\d+/[a-z0-9/_\-]+)[\"']", src))
    assert endpoints == {"/v4/product/info/limit"}
    # bare mentions (docs/comments) may only name the quota read, or the upload
    # endpoint the --dry-run exits before
    assert set(re.findall(r"/v\d+/[a-z0-9/_\-]+", src)) <= {"/v4/product/info/limit", "/v3/product/import"}
    assert ui.READ_ONLY_ENDPOINTS == frozenset({"/v4/product/info/limit"})
    assert not any(m in src for m in ("import requests", "urllib.request", "http.client", "urlopen"))
    assert "NewProductAddingTab" in src
    assert ui.NP_CAPTION.startswith("URL targets and priorities are saved for the add run")
    assert ui.ALLOC_SECTION_TITLE == "Daily plan allocations (offers)"


def test_urls_entry_validation_and_parse():
    assert ui.urls_entry_ok("") and ui.urls_entry_ok("120")
    assert not ui.urls_entry_ok("-1") and not ui.urls_entry_ok("1.5") and not ui.urls_entry_ok("abc")
    assert not ui.urls_entry_ok("1" * (ui.URLS_MAX_DIGITS + 1))
    assert ui.parse_urls_to_add(" 007 ") == 7
    for bad in ("", "  ", "-3", "x", None):
        with pytest.raises(ValueError):
            ui.parse_urls_to_add(bad)


def test_priority_labels_and_fmt():
    assert ui.priority_from_label("Very high") == "very_high"
    assert ui.priority_from_label("Very low") == "very_low"
    assert ui.priority_from_label("nope") is None
    assert ui.np_fmt(None) == "—" and ui.np_fmt(1234) == "1,234" and ui.np_fmt(True) == "—"


def test_np_dirty_and_save_state():
    import new_product_plan as npp
    saved = npp.default_plan()
    assert ui.np_save_state(saved, saved) == ([], "")
    same = npp.set_entry(saved, "M&S", "Pajama", priority="medium", urls_to_add=0)
    assert not ui.np_is_dirty(same, saved)  # explicit defaults == missing entry
    changed = npp.set_entry(saved, "M&S", "Pajama", urls_to_add=5)
    assert ui.np_is_dirty(changed, saved)
    assert ui.np_save_state(changed, saved) == ([], "Unsaved changes.")
    bad = {"version": 1, "categories": {"H&M/All": {"urls_to_add": 3}}}
    errors, note = ui.np_save_state(bad, saved)
    assert errors and note.startswith("Can't save")


def test_np_rows_sort_and_add_buttons():
    import new_product_plan as npp
    inv = {"brands": {"M&S": {"categories": {
        "Pajama": {"live_products": 10, "crawled_not_live_products": 4, "discovered_not_live_products": 3},
        "Socks": {"live_products": 2, "crawled_not_live_products": 1}}}}}
    plan = npp.set_entry(npp.default_plan(), "M&S", "Socks", priority="very_high")
    rows = npp.build_rows(inv, plan)
    assert rows[0]["category"] == "Socks" and rows[-1]["brand"] == "H&M"
    pj = next(r for r in rows if r["category"] == "Pajama")
    assert (pj["total_urls"], pj["eligible_urls"]) == (17, 7)
    assert npp.add_all(pj) == 7 and npp.add_half(pj) == 3
    assert ui.np_fmt(rows[-1]["eligible_urls"]) == "—"
