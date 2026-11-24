"""Offline tests for planner_ui.pyw's pure Daily tasks builders -- no Tk, no schtasks."""
import importlib.machinery
import importlib.util
import os
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
