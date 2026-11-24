"""Offline tests for planner_ui.pyw's pure Daily tasks builders -- no Tk, no schtasks."""
import ast
import importlib.machinery
import importlib.util
import os
import re
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "planner_ui.pyw")
NOW = datetime(2026, 11, 24, 11, 0, 0)


def _load_app():
    loader = importlib.machinery.SourceFileLoader("planner_ui", APP_PATH)
    spec = importlib.util.spec_from_loader("planner_ui", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ui = _load_app()


def _task(name, **kw):
    t = {"name": name, "path": "\\" + name, "status": "Ready", "state": "Enabled",
         "last_run": "11/24/2026 4:00:00 AM", "last_result_code": 0, "last_result_text": "Success",
         "ok": True, "next_run": "11/25/2026 4:00:00 AM", "action": None, "bat": None}
    t.update(kw)
    return t


class _WeirdError:
    def __str__(self):
        return "weird error object"


def _status(**dr_kw):
    daily_run = {
        "date": "2026-11-24", "state": "completed", "started_at": "2026-11-24T04:00:01",
        "finished_at": "2026-11-24T09:12:00",
        "steps": [
            {"name": "Stockout re-check (recheck_stockouts.py)", "script": "recheck_stockouts.py",
             "status": "timeout", "seconds": 3600.2},
            {"name": "Upload (upload_to_ozon.py)", "script": "upload_to_ozon.py", "status": "failed",
             "seconds": 723},
            {"name": "Crawl (crawler.py)", "script": "crawler.py", "status": "ok", "seconds": 45.4},
            {"name": "Auto-translate (auto_translate.py)", "script": "auto_translate.py",
             "status": "timeout", "seconds": 10800},
        ],
        "attention": ["Upload step failed", "", None],
        "created_today": 412, "plan_total": 500,
        "content_summary": "content: avg 63.2, 120 groups improved",
        "upload_totals": "upload: 412 created, 30 updated",
        "is_today": True, "stale": False, "failed_steps": [],
    }
    daily_run.update(dr_kw)
    return {
        "collected_at": NOW.isoformat(), "today": "2026-11-24",
        "tasks": [
            _task("MandsOzonDailyRun"),
            _task("OzonSales_DailyHMStockUpdate", last_result_code=1,
                  last_result_text="Error (code 1 / 0x1)", ok=False,
                  last_run="11/24/2026 7:00:00 AM", next_run="11/25/2026 7:00:00 AM"),
            _task("MandsOzonQuotaFill", status="Running", last_result_code=0x41301,
                  last_result_text="Running", ok=None, next_run="11/25/2026 10:15:00 AM"),
            _task("MandsOzonDailyReport", last_run=None, last_result_code=0x41303,
                  last_result_text="Never run", ok=None, next_run="11/24/2026 10:00:00 PM"),
            _task("MandsOzonOddTime", last_run="vendredi 4h", next_run="bientôt"),
        ],
        "daily_run": daily_run,
        "last_runs": [],
        "errors": ["schtasks timed out after 30s", _WeirdError(), "schtasks timed out after 30s"],
        "tasks_error": "schtasks timed out after 30s",
    }


def _levels(rows):
    return {r["name"]: r["level"] for r in rows}


def test_import_still_tk_free():
    assert "tkinter" not in ui.__dict__
    for fn in ("task_result_level", "parse_task_time", "task_is_stale", "build_task_rows",
               "step_level", "build_step_rows", "build_run_summary", "attention_items",
               "stale_banner", "error_lines", "level_color"):
        assert callable(getattr(ui, fn))


def test_task_levels_from_fixture():
    rows = ui.build_task_rows(_status(), now=NOW)
    lv = _levels(rows)
    assert lv["MandsOzonDailyRun"] == "good"
    assert lv["OzonSales_DailyHMStockUpdate"] == "critical"
    assert lv["MandsOzonQuotaFill"] == "running"
    assert lv["MandsOzonDailyReport"] == "warning"
    assert lv["MandsOzonOddTime"] == "good"  # unparseable time is never stale
    for r in rows:
        assert r["tag"] == r["level"]
        assert set(r) == {"name", "next_run", "last_run", "result", "status", "level", "tag"}
    never = next(r for r in rows if r["name"] == "MandsOzonDailyReport")
    assert never["last_run"] == "never"
    odd = next(r for r in rows if r["name"] == "MandsOzonOddTime")
    assert "(stale)" not in odd["result"]


def test_task_rows_sorted_by_next_run_then_name():
    names = [r["name"] for r in ui.build_task_rows(_status(), now=NOW)]
    assert names == ["MandsOzonDailyReport", "MandsOzonDailyRun", "OzonSales_DailyHMStockUpdate",
                     "MandsOzonQuotaFill", "MandsOzonOddTime"]


def test_missed_next_run_is_stale_warning():
    t = _task("MandsOzonDailyRun", last_run="11/23/2026 4:00:00 AM", next_run="11/24/2026 4:00:00 AM")
    assert ui.task_is_stale(t, NOW)
    assert ui.task_result_level(t, NOW) == "warning"
    row = ui.build_task_rows({"tasks": [t]}, now=NOW)[0]
    assert row["result"] == "Success (stale)"
    # within the 10 min grace: not stale
    t2 = _task("X", next_run="11/24/2026 10:55:00 AM")
    assert not ui.task_is_stale(t2, NOW)


def test_26h_rule_only_without_next_run():
    old = _task("X", last_run="11/22/2026 4:00:00 AM", next_run=None)
    assert ui.task_is_stale(old, NOW)
    assert ui.task_result_level(old, NOW) == "warning"
    fresh = _task("X", last_run="11/24/2026 4:00:00 AM", next_run=None)
    assert not ui.task_is_stale(fresh, NOW)
    unparseable_last = _task("X", last_run="??", next_run="11/20/2026 4:00:00 AM")
    assert not ui.task_is_stale(unparseable_last, NOW)


def test_running_and_disabled_variants():
    assert ui.task_result_level(_task("X", status="RUNNING", last_result_code=0), NOW) == "running"
    assert ui.task_result_level(_task("X", status="Çalışıyor"), NOW) == "running"
    assert ui.task_result_level(_task("X", last_result_code=0x41302), NOW) == "warning"
    assert ui.task_result_level(_task("X", state="Disabled"), NOW) == "warning"
    assert ui.task_result_level(_task("X", state="Devre dışı"), NOW) == "warning"
    assert ui.task_result_level(_task("X", ok=None, last_result_code=0x41300), NOW) == "warning"


@pytest.mark.parametrize("s, expected", [
    ("11/24/2026 4:00:00 AM", datetime(2026, 11, 24, 4, 0, 0)),
    ("11/24/2026 16:05:00", datetime(2026, 11, 24, 16, 5, 0)),
    ("24.11.2026 16:05:00", datetime(2026, 11, 24, 16, 5, 0)),
    ("24/11/2026 16:05:00", datetime(2026, 11, 24, 16, 5, 0)),
    ("2026-11-24 16:05:00", datetime(2026, 11, 24, 16, 5, 0)),
    ("garbage", None), ("", None), (None, None), (12345, None),
])
def test_parse_task_time(s, expected):
    assert ui.parse_task_time(s) == expected


def test_step_levels_and_rows():
    rows = ui.build_step_rows(_status()["daily_run"])
    by = {r["script"]: r for r in rows}
    assert by["recheck_stockouts.py"]["level"] == "warning"
    assert by["recheck_stockouts.py"]["result"] == "TIMED OUT (normal for this step)"
    assert by["upload_to_ozon.py"]["level"] == "critical"
    assert by["upload_to_ozon.py"]["result"] == "FAILED"
    assert by["crawler.py"]["level"] == "good"
    assert by["crawler.py"]["result"] == "ok"
    assert by["auto_translate.py"]["level"] == "critical"
    assert by["auto_translate.py"]["result"] == "TIMED OUT"
    assert by["upload_to_ozon.py"]["seconds"] == "12m 03s"
    assert by["crawler.py"]["seconds"] == "0m 45s"
    assert by["auto_translate.py"]["seconds"] == "3h 00m 00s"
    assert ui.step_level({"status": "weird"}) == "warning"
    assert ui.step_level({"status": "crashed"}) == "critical"
    assert ui.step_level({"ok": False}) == "critical"
    assert ui.build_step_rows({"steps": [{"name": "x", "seconds": None}]})[0]["seconds"] == "—"


def test_run_summary_and_attention():
    summary = {label: (value, level) for label, value, level in ui.build_run_summary(_status()["daily_run"])}
    assert summary["State"] == ("completed", "good")
    assert summary["Plan created/total"] == ("412 / 500", "good")
    assert summary["Content"][1] == "good"
    assert summary["Attention"] == ("1", "warning")
    assert ui.attention_items(_status()["daily_run"]) == ["Upload step failed"]

    running = {l: (v, lv) for l, v, lv in ui.build_run_summary(
        {"state": "running", "finished_at": "x", "created_today": None, "plan_total": 500})}
    assert running["State"] == ("running", "running")
    assert running["Finished"][0] == "—"
    assert running["Plan created/total"] == ("? / 500", "warning")
    assert running["Content"] == ("content status unknown", "warning")
    assert running["Upload totals"] == ("upload status unknown", "warning")
    assert running["Attention"] == ("0", "good")
    for state, lvl in (("failed", "critical"), ("blocked", "critical"), ("unknown", "warning")):
        assert ui.build_run_summary({"state": state})[0] == ("State", state, lvl)


def test_stale_banner():
    stale = _status(date="2026-11-23", is_today=False, stale=True)
    assert ui.stale_banner(stale) == "Latest daily run is not from today (file date 2026-11-23, today 2026-11-24)"
    no_date = _status(date=None, is_today=False, stale=True)
    assert "file has no date" in ui.stale_banner(no_date)
    assert ui.stale_banner(_status()) is None
    missing = {"today": "2026-11-24", "daily_run": {"state": "unknown", "error": "daily_run_status.json not found",
                                                    "is_today": False, "stale": False, "failed_steps": []}}
    assert ui.stale_banner(missing) is None


def test_error_lines_dedup_and_non_str():
    assert ui.error_lines(_status()) == ["schtasks timed out after 30s", "weird error object"]
    assert ui.error_lines({"tasks_error": "only this"}) == ["only this"]


def test_level_color():
    for key, hexval in ui.STATUS.items():
        assert ui.level_color(key) == hexval
    assert ui.level_color("good") == "#3fa66a"
    assert ui.level_color("critical") == "#e5534b"
    assert ui.level_color("running") == "#4c8bf5"
    assert ui.level_color("warning") == "#d9a43a"
    assert ui.level_color("muted") == ui.MUTED
    assert ui.level_color(None) == ui.MUTED
    assert ui.level_color(["unhashable"]) == ui.MUTED


GARBAGE = [None, {}, [], "text", 42, 3.5, object(), {"tasks": "nope", "daily_run": "x", "errors": 5},
           {"tasks": [None, 1, "x", {"name": None, "last_result_code": "0x1", "ok": "yes"}],
            "daily_run": {"steps": [None, "x", {"status": None, "seconds": "12"}],
                          "attention": "not a list", "state": 7, "is_today": None, "stale": "yes"},
            "errors": [None, "", 3, _WeirdError()]}]


@pytest.mark.parametrize("bad", GARBAGE)
def test_builders_never_raise(bad):
    assert isinstance(ui.build_task_rows(bad), list)
    assert isinstance(ui.build_task_rows(bad, now="not a datetime"), list)
    assert ui.task_result_level(bad) in ui.STATUS
    assert isinstance(ui.task_is_stale(bad, NOW), bool)
    assert ui.step_level(bad) in ui.STATUS
    assert isinstance(ui.build_step_rows(bad), list)
    assert isinstance(ui.build_run_summary(bad), list)
    assert isinstance(ui.attention_items(bad), list)
    banner = ui.stale_banner(bad)
    assert banner is None or isinstance(banner, str)
    assert all(isinstance(e, str) for e in ui.error_lines(bad))
    assert isinstance(ui.level_color(bad if isinstance(bad, str) else None), str)
    dr = bad.get("daily_run") if isinstance(bad, dict) else bad
    assert isinstance(ui.build_step_rows(dr), list)
    assert isinstance(ui.build_run_summary(dr), list)


def test_garbage_details():
    bad = GARBAGE[-1]
    rows = ui.build_task_rows(bad)
    assert len(rows) == 4 and all(r["level"] in ui.STATUS for r in rows)
    assert ui.error_lines(bad) == ["3", "weird error object"]
    assert ui.stale_banner(bad).startswith("Latest daily run is not from today")


# --- DailyTasksTab: refresh/threading logic (fake app, no Tk) -------------------

class _FakeRoot:
    def __init__(self):
        self.pending = {}
        self.cancelled = []
        self._n = 0

    def after(self, ms, fn):
        self._n += 1
        aid = f"after#{self._n}"
        self.pending[aid] = (ms, fn)
        return aid

    def after_cancel(self, aid):
        self.cancelled.append(aid)
        self.pending.pop(aid, None)


class _FakeWidget:
    def __init__(self):
        self.config = {}

    def configure(self, **kw):
        self.config.update(kw)


class _FakeApp:
    def __init__(self):
        self.root = _FakeRoot()
        self.posted = []

    def post(self, fn, *args):
        self.posted.append((fn, args))


class _HeldThread:
    """Stands in for threading.Thread: records the target, runs it only on demand."""
    started = []

    def __init__(self, target=None, daemon=None):
        assert daemon is True
        self.target = target

    def start(self):
        _HeldThread.started.append(self)


def _fake_tab(monkeypatch):
    _HeldThread.started = []
    monkeypatch.setattr(ui.threading, "Thread", _HeldThread)
    tab = ui.DailyTasksTab.__new__(ui.DailyTasksTab)
    tab.app = _FakeApp()
    tab.busy = False
    tab._after_id = None
    tab.status = None
    tab.refresh_btn = _FakeWidget()
    tab.collected_label = _FakeWidget()
    tab.rendered = []
    tab.render = lambda status: tab.rendered.append(status)
    return tab


def test_refresh_never_overlaps_and_posts_result(monkeypatch):
    tab = _fake_tab(monkeypatch)
    calls = []
    monkeypatch.setattr(ui.task_status, "collect_status", lambda: calls.append(1) or _status())
    tab._schedule(ui.DAILY_FIRST_REFRESH_MS)
    first = tab._after_id
    tab.refresh()
    assert tab.busy and tab.refresh_btn.config["state"] == "disabled"
    assert first in tab.app.root.cancelled and tab._after_id is None
    tab.refresh()  # busy -> no second collect
    tab.refresh()
    assert len(_HeldThread.started) == 1
    _HeldThread.started[0].target()  # worker: collects, then hands back via app.post only
    assert calls == [1] and tab.rendered == [] and tab.busy
    fn, args = tab.app.posted[0]
    fn(*args)  # UI thread drains the queue
    assert not tab.busy and tab.refresh_btn.config["state"] == "normal"
    assert tab.rendered and tab.rendered[0]["tasks"]
    assert len(tab.app.root.pending) == 1
    assert list(tab.app.root.pending.values())[0][0] == ui.DAILY_REFRESH_MS == 60_000


def test_manual_refresh_keeps_one_pending_timer(monkeypatch):
    tab = _fake_tab(monkeypatch)
    monkeypatch.setattr(ui.task_status, "collect_status", _status)
    for _ in range(3):
        tab.refresh()
        _HeldThread.started[-1].target()
        fn, args = tab.app.posted.pop()
        fn(*args)
        assert len(tab.app.root.pending) == 1
    assert len(_HeldThread.started) == 3


def test_collect_failure_becomes_error_entry(monkeypatch):
    tab = _fake_tab(monkeypatch)

    def boom():
        raise RuntimeError("schtasks exploded")

    monkeypatch.setattr(ui.task_status, "collect_status", boom)
    tab.refresh()
    _HeldThread.started[0].target()
    fn, args = tab.app.posted[0]
    fn(*args)
    errs = ui.error_lines(tab.rendered[0])
    assert errs and "schtasks exploded" in errs[0]
    assert not tab.busy and len(tab.app.root.pending) == 1


def test_render_failure_still_clears_busy(monkeypatch):
    tab = _fake_tab(monkeypatch)

    def bad_render(status):
        raise ValueError("bad")

    tab.render = bad_render
    tab.busy = True
    tab._apply({})
    assert not tab.busy and len(tab.app.root.pending) == 1


def test_collect_daily_status_non_dict(monkeypatch):
    monkeypatch.setattr(ui.task_status, "collect_status", lambda: None)
    out = ui.collect_daily_status()
    assert out["tasks"] == [] and "not a dict" in out["errors"][0]


def test_collected_text():
    assert ui.collected_text({"collected_at": "2026-11-24T11:02:03"}) == "collected 11:02:03"
    assert ui.collected_text({}) == "collected —"
    assert ui.collected_text({"collected_at": "garbage"}) == "collected —"
    assert ui.collected_text(None) == "collected —"


# --- source guards (ast / grep on planner_ui.pyw) --------------------------------

with open(APP_PATH, encoding="utf-8") as _f:
    SOURCE = _f.read()
TREE = ast.parse(SOURCE)
FORBIDDEN_CALL_NAMES = {"system", "run_script", "command_argv", "_run_bg", "Popen", "check_call",
                        "check_output", "startfile"}


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        base = f.value.id if isinstance(f.value, ast.Name) else None
        return base, f.attr
    if isinstance(f, ast.Name):
        return None, f.id
    return None, None


def _daily_nodes():
    lines = SOURCE.splitlines()
    start = next(i for i, ln in enumerate(lines, 1) if ln.startswith("# --- Daily tasks (pure)"))
    end = next(i for i, ln in enumerate(lines, 1) if ln.startswith("# --- Tabs"))
    pure = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and start < n.lineno < end]
    cls = [n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "DailyTasksTab"]
    assert len(cls) == 1 and len(pure) >= 10
    return pure + cls


def test_daily_tasks_code_starts_no_process():
    for node in _daily_nodes():
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            base, name = _call_name(call)
            assert base not in ("subprocess", "os"), f"{node.name}: {base}.{name}"
            assert name not in FORBIDDEN_CALL_NAMES, f"{node.name}: {name}"


def test_source_has_no_scheduler_write_switches():
    low = SOURCE.lower()
    for switch in ("/run", "/change", "/end", "/create", "/delete"):
        assert switch not in low, switch
    assert not re.search(r"schtasks[^\n]*/(run|change|end|create|delete)", low)


def test_daily_tab_registered_and_order_unchanged():
    assert ui.TAB_BUILDERS["Daily tasks"] is ui.DailyTasksTab
    assert ui.TAB_NAMES == ("Overview", "Daily tasks", "Prioritization")
    assert list(ui.TAB_BUILDERS) == list(ui.TAB_NAMES)
    assert ui.DEFAULT_TAB == "Prioritization"
    assert set(ui.COMMANDS) == {"catalog_status", "inventory", "walk", "preview", "account_snapshot"}
    assert set(ui.TIMEOUTS) == set(ui.COMMANDS)


def test_daily_tab_has_exactly_one_button_refresh():
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "DailyTasksTab")
    buttons = [c for c in ast.walk(cls)
               if isinstance(c, ast.Call) and _call_name(c)[1] in ("button", "Button", "Checkbutton")]
    assert len(buttons) == 1
    assert isinstance(buttons[0].args[1], ast.Constant) and buttons[0].args[1].value == "Refresh"


# --- optional real-Tk smoke test (skipped without a display) ---------------------

def test_daily_tab_renders_in_real_tk():
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

        frame = tkinter.Frame(root, bg=ui.BG)
        tab = ui.DailyTasksTab(App(), frame)
        tab._apply(_status(is_today=False, stale=True, date="2026-11-23"))
        assert len(tab.jobs.get_children()) == 5
        assert len(tab.steps.get_children()) == 4
        assert tab.banner.winfo_manager() == "pack"
        assert tab.errors.winfo_manager() == "pack"
        assert tab.collected_label.cget("text") == "collected 11:00:00"
        tags = [tab.steps.item(i, "tags")[0] for i in tab.steps.get_children()]
        assert tags == ["warning", "critical", "good", "critical"]

        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)

        buttons = [w for w in walk(frame) if isinstance(w, tkinter.Button)]
        assert [b.cget("text") for b in buttons] == ["Refresh"]
        tab._apply({"collected_at": NOW.isoformat(), "today": "2026-11-24",
                    "daily_run": {"is_today": True, "stale": False}, "tasks": [], "errors": []})
        assert tab.banner.winfo_manager() == "" and tab.errors.winfo_manager() == ""
        tab._cancel_timer()
    finally:
        root.destroy()
