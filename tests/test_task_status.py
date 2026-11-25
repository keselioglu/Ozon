"""Offline tests for task_status.py (task #54). No real schtasks call."""
import json
import subprocess
from datetime import datetime
from pathlib import Path

import task_status as ts

HEADER = ('"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time",'
          '"Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State",'
          '"Schedule Type","Start Time"')


def _row(name, next_run, status, last_run, result, action, state="Enabled"):
    return (f'"PC","{name}","{next_run}","{status}","Interactive only","{last_run}",'
            f'"{result}","PC\\\\Tugberk","{action}","C:\\\\Git\\\\Ozon","N/A","{state}",'
            f'"Daily ","4:00:00 AM"')


EN_CSV = "\n".join([
    HEADER,
    _row("\\MandsOzonDailyRun", "11/25/2026 4:00:00 AM", "Ready", "11/24/2026 4:00:00 AM",
         "0", "C:\\Git\\Ozon\\run_daily.bat"),
    _row("\\MandsOzonQuotaFill", "11/25/2026 10:15:00 AM", "Running", "11/24/2026 10:15:00 AM",
         "267009", "C:\\Git\\Ozon\\run_quota_fill.bat"),
    _row("\\MandsOzonQuotaFill", "11/26/2026 10:15:00 AM", "Running", "11/24/2026 10:15:00 AM",
         "267009", "C:\\Git\\Ozon\\run_quota_fill.bat"),  # second trigger -> dedupe
    _row("\\MandsOzonDailyReport", "N/A", "Ready", "11/30/1999 12:00:00 AM",
         "267011", "C:\\Git\\Ozon\\run_daily_report.bat"),
    "",
    HEADER,  # repeated header for the next folder
    _row("\\Microsoft\\Windows\\Defrag\\ScheduledDefrag", "N/A", "Ready",
         "11/20/2026 1:00:00 AM", "0", "%windir%\\system32\\defrag.exe -c"),
    _row("\\OzonSales_DailyHMStockUpdate", "11/25/2026 7:00:00 AM", "Ready",
         "11/24/2026 7:00:00 AM", "1", "C:\\Git\\Ozon\\run_hm_stock_update.bat"),
]) + "\n"

TR_CSV = (
    '"Ana Bilgisayar Adı","Görev Adı","Sonraki Çalışma Zamanı","Durum","Oturum Açma Modu",'
    '"Son Çalışma Zamanı","Son Sonuç","Yazar","Çalıştırılacak Görev"\n'
    '"PC","\\MandsOzonQuotaTopup","Yok","Hazır","Etkileşimli","24.11.2026 05:00:00","0",'
    '"PC\\Tugberk","C:\\Git\\Ozon\\run_quota_topup.bat"\n'
    '"PC","\\Other","Yok","Hazır","Etkileşimli","24.11.2026 05:00:00","0","PC\\T","x.exe"\n'
)

# Unknown header names -> positional English /v fallback.
POS_CSV = (
    '"H","Nom","Prochaine","Statut","Mode","Dernier","Resultat","Auteur","Tache"\n'
    '"PC","\\MandsOzonTodaysStockCheck","N/A","Prêt","I","24/11/2026 06:00:00","0x41301",'
    '"PC\\T","C:\\Git\\Ozon\\run_todays_stock_check.bat"\n'
)


def _by_name(rows):
    return {r["name"]: r for r in rows}


# ---------------------------------------------------------------- parsing

def test_parse_english_fixture_filters_dedupes_and_skips_header():
    rows = ts.parse_schtasks_csv(EN_CSV)
    names = [r["name"] for r in rows]
    assert names == ["MandsOzonDailyRun", "MandsOzonQuotaFill", "MandsOzonDailyReport",
                     "OzonSales_DailyHMStockUpdate"]
    by = _by_name(rows)
    run = by["MandsOzonDailyRun"]
    assert run["ok"] is True and run["last_result_text"] == "Success"
    assert run["bat"] == "run_daily.bat"
    assert run["status"] == "Ready" and run["next_run"] == "11/25/2026 4:00:00 AM"
    assert by["MandsOzonQuotaFill"]["last_result_text"] == "Running"
    assert by["MandsOzonQuotaFill"]["next_run"] == "11/25/2026 10:15:00 AM"  # first row kept
    rep = by["MandsOzonDailyReport"]
    assert rep["next_run"] is None and rep["last_run"] is None
    assert rep["last_result_text"] == "Never run"
    hm = by["OzonSales_DailyHMStockUpdate"]
    assert hm["ok"] is False and hm["last_result_code"] == 1


def test_parse_turkish_headers():
    rows = ts.parse_schtasks_csv(TR_CSV)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "MandsOzonQuotaTopup"
    assert r["status"] == "Hazır"
    assert r["next_run"] is None  # 'Yok'
    assert r["last_result_code"] == 0 and r["ok"] is True
    assert r["bat"] == "run_quota_topup.bat"


def test_parse_positional_fallback():
    rows = ts.parse_schtasks_csv(POS_CSV)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "MandsOzonTodaysStockCheck"
    assert r["status"] == "Prêt"
    assert r["next_run"] is None
    assert r["last_result_text"] == "Running"
    assert r["last_run"] == "24/11/2026 06:00:00"


def test_parse_empty_and_garbage():
    assert ts.parse_schtasks_csv("") == []
    assert ts.parse_schtasks_csv(None) == []
    assert ts.parse_schtasks_csv("just one line") == []


def test_describe_result_codes():
    assert ts.describe_result("0") == (0, "Success", True)
    assert ts.describe_result("267009")[1] == "Running"
    assert ts.describe_result("0x41301")[1] == "Running"
    assert ts.describe_result("267011")[1] == "Never run"
    assert ts.describe_result("N/A") == (None, "unknown", None)
    assert ts.describe_result(None) == (None, "unknown", None)
    code, text, ok = ts.describe_result("-2147024894")  # 0x80070002
    assert code == 0x80070002 and text == "File not found" and ok is False
    assert ts.describe_result("0x8004131F")[1] == "An instance is already running"
    code, text, ok = ts.describe_result("12345")
    assert ok is False and "12345" in text and "0x3039" in text


# ---------------------------------------------------------------- run status

def test_run_status_missing(tmp_path):
    st = ts.load_run_status(tmp_path / "daily_run_status.json", today="2026-11-24")
    assert st["state"] == "unknown" and "not found" in st["error"]


def test_run_status_corrupt(tmp_path):
    p = tmp_path / "daily_run_status.json"
    p.write_text("{not json", encoding="utf-8")
    st = ts.load_run_status(p, today="2026-11-24")
    assert st["state"] == "unknown" and st["error"]
    p.write_text("[1, 2]", encoding="utf-8")
    assert ts.load_run_status(p, today="2026-11-24")["state"] == "unknown"


def _status(date, state="completed", steps=None):
    return {"date": date, "state": state, "started_at": f"{date}T04:00:00",
            "finished_at": f"{date}T07:00:00", "steps": steps or [], "summary": "ok",
            "attention": [], "created_today": 120, "plan_total": 500,
            "content_summary": None, "upload_totals": None}


def test_run_status_today_and_stale(tmp_path):
    p = tmp_path / "daily_run_status.json"
    steps = [{"name": "crawl", "status": "ok", "seconds": 1},
             {"name": "recheck", "status": "timeout", "seconds": 3600},
             {"name": "upload", "status": "failed", "seconds": 2}]
    p.write_text(json.dumps(_status("2026-11-24", steps=steps)), encoding="utf-8")
    st = ts.load_run_status(p, today="2026-11-24")
    assert st["is_today"] is True and st["stale"] is False
    assert st["state"] == "completed" and st["plan_total"] == 500
    assert st["failed_steps"] == ["recheck", "upload"]

    p.write_text(json.dumps(_status("2026-11-23", state="weird")), encoding="utf-8")
    st = ts.load_run_status(p, today="2026-11-24")
    assert st["is_today"] is False and st["stale"] is True
    assert st["state"] == "unknown"


# ---------------------------------------------------------------- last runs

def test_last_run_rows(tmp_path):
    assert ts.load_last_run_rows(tmp_path) == ([], [])
    (tmp_path / "hm_stock_last_run.json").write_text(json.dumps(
        {"date": "2026-11-24T07:01:00", "decisions": {"HM-1": {}, "HM-2": {}},
         "read_failed": ["HM-3"]}), encoding="utf-8")
    (tmp_path / "catalog_status.json").write_text(json.dumps(
        {"built": "x", "runs": {"price_ms": {"when": "2026-11-24", "ok": 10, "fresh": True},
                                "stock_hm": {"when": None, "ok": 0, "fresh": False}}}),
        encoding="utf-8")
    rows, errors = ts.load_last_run_rows(tmp_path)
    assert errors == []
    assert rows[0] == {"source": "hm_stock_last_run.json", "job": "hm_stock",
                       "when": "2026-11-24T07:01:00", "detail": "decisions=2, read_failed=1"}
    jobs = {r["job"]: r for r in rows[1:]}
    assert jobs["price_ms"]["detail"] == "ok=10, fresh=True"
    assert "stock_hm" in jobs


def test_last_run_rows_corrupt(tmp_path):
    (tmp_path / "catalog_status.json").write_text("{{", encoding="utf-8")
    rows, errors = ts.load_last_run_rows(tmp_path)
    assert rows == [] and len(errors) == 1


# ---------------------------------------------------------------- collect_status

NOW = datetime(2026, 11, 24, 12, 0, 0)


class _Res:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _patch_run(monkeypatch, fn):
    calls = []

    def fake(args, **kw):
        calls.append(list(args))
        return fn(args, **kw)
    monkeypatch.setattr(ts.subprocess, "run", fake)
    return calls


def test_collect_status_timeout(monkeypatch, tmp_path):
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout"))
    _patch_run(monkeypatch, boom)
    st = ts.collect_status(NOW, repo_dir=tmp_path)
    assert st["tasks"] == [] and "timed out" in st["tasks_error"]
    assert st["tasks_error"] in st["errors"]
    assert st["daily_run"]["state"] == "unknown"
    assert st["today"] == "2026-11-24"


def test_collect_status_not_found(monkeypatch, tmp_path):
    def boom(args, **kw):
        raise FileNotFoundError("schtasks")
    _patch_run(monkeypatch, boom)
    st = ts.collect_status(NOW, repo_dir=tmp_path)
    assert st["tasks"] == [] and st["tasks_error"]


def test_collect_status_nonzero_returncode(monkeypatch, tmp_path):
    _patch_run(monkeypatch, lambda a, **k: _Res(1, b"", b"ERROR: Access is denied."))
    st = ts.collect_status(NOW, repo_dir=tmp_path)
    assert "Access is denied" in st["tasks_error"]


def test_collect_status_success_only_queries(monkeypatch, tmp_path):
    calls = _patch_run(monkeypatch, lambda a, **k: _Res(0, EN_CSV.encode("utf-8")))
    (tmp_path / "daily_run_status.json").write_text(json.dumps(_status("2026-11-24")),
                                                    encoding="utf-8")
    st = ts.collect_status(NOW, repo_dir=tmp_path)
    assert "tasks_error" not in st and st["errors"] == []
    assert len(st["tasks"]) == 4
    assert st["daily_run"]["is_today"] is True
    assert calls == [["schtasks", "/query", "/fo", "CSV", "/v"]]


def test_collect_status_cp_decoding(monkeypatch, tmp_path):
    _patch_run(monkeypatch, lambda a, **k: _Res(0, TR_CSV.encode("cp1254")))
    st = ts.collect_status(NOW, repo_dir=tmp_path)
    assert [t["name"] for t in st["tasks"]] == ["MandsOzonQuotaTopup"]


def test_no_mutating_schtasks_switches_in_source():
    src = open(ts.__file__, encoding="utf-8").read().lower()
    for bad in ("/create", "/change", "/run\"", "/run'", "/delete", "/end"):
        assert bad not in src


# ---------------------------------------------------------------- task metrics (task #101)

def _jsonl(path, *records, raw_tail=None):
    lines = [json.dumps(r) for r in records]
    if raw_tail is not None:
        lines.append(raw_tail)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


MS_OLD = {"date": "2026-11-23", "finished_at": "2026-11-23T01:30:00", "pages_checked": 1,
          "products_checked": 2, "comparison": {"increased": 0, "decreased": 0}}
MS_REC = {"date": "2026-11-24", "finished_at": "2026-11-24T01:40:00", "pages_checked": 900,
          "pages_failed": 3, "products_checked": 4100, "products_pushed_ok": 4050,
          "comparison": {"increased": 12, "decreased": 30, "unchanged": 4000}}
HM_REC = {"date": "2026-11-24", "finished_at": "2026-11-24T02:10:00", "products_read": 250,
          "offers_live": 1800, "stock_changed": 44}
REFRESH_REC = {"date": "2026-11-24", "finished_at": "2026-11-24T05:20:00", "urls_checked": 700,
               "urls_failed": 2, "prices_checked": 3900, "prices_changed": 15}
UPDATE_REC = {"date": "2026-11-23", "finished_at": "2026-11-23T05:40:00", "pushed_ok": 3800,
              "price_changed_ok": 120, "limit_changed_ok": 340}


def _all_fields(entry):
    return all(k in entry for k in ts.TASK_METRIC_FIELDS)


def test_task_metrics_normal_files(tmp_path):
    _jsonl(tmp_path / "stock_update_history.jsonl", MS_OLD, MS_REC)
    _jsonl(tmp_path / "hm_stock_update_history.jsonl", HM_REC)
    _jsonl(tmp_path / "price_refresh_history.jsonl", REFRESH_REC)
    _jsonl(tmp_path / "price_update_history.jsonl", UPDATE_REC)
    m, errors = ts.load_task_metrics(tmp_path)
    assert errors == []
    assert set(m) == {"ms_stock", "hm_stock", "price"}
    assert all(_all_fields(e) for e in m.values())

    ms = m["ms_stock"]
    assert (ms["urls_checked"], ms["sku_stock_checked"], ms["sku_stock_updated"]) == (900, 4100, 42)
    assert (ms["pages_failed"], ms["products_pushed_ok"]) == (3, 4050)
    assert ms["prices_checked"] is None and ms["sku_price_updated"] is None
    assert ms["date"] == "2026-11-24" and ms["finished_at"] == "2026-11-24T01:40:00"

    hm = m["hm_stock"]
    assert (hm["urls_checked"], hm["sku_stock_checked"], hm["sku_stock_updated"]) == (250, 1800, 44)
    assert hm["campaign_limit_updated"] is None

    pr = m["price"]
    assert (pr["urls_checked"], pr["prices_checked"]) == (700, 3900)
    assert (pr["sku_price_updated"], pr["campaign_limit_updated"]) == (120, 340)
    assert pr["sku_stock_checked"] is None and pr["sku_stock_updated"] is None
    assert pr["refresh_date"] == "2026-11-24" and pr["update_date"] == "2026-11-23"
    assert pr["refresh_finished_at"] == "2026-11-24T05:20:00"
    assert pr["update_finished_at"] == "2026-11-23T05:40:00"


def test_task_metrics_missing_files(tmp_path):
    assert ts.load_task_metrics(tmp_path) == ({}, [])


def test_task_metrics_never_reads_price_history(tmp_path, monkeypatch):
    (tmp_path / "price_history.jsonl").write_text("{}\n", encoding="utf-8")
    _jsonl(tmp_path / "price_update_history.jsonl", UPDATE_REC)
    seen = []
    real_open = open

    def spy(file, *a, **k):
        seen.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    ts.load_task_metrics(tmp_path)
    assert seen and not any(Path(s).name == "price_history.jsonl" for s in seen)


def test_task_metrics_corrupt_last_line_falls_back(tmp_path):
    _jsonl(tmp_path / "stock_update_history.jsonl", MS_OLD, MS_REC, raw_tail='{"date": "2026-11-25", "pages')
    (tmp_path / "hm_stock_update_history.jsonl").write_text(
        json.dumps(HM_REC) + "\n\n   \nnot json\n[1, 2]\n", encoding="utf-8")
    m, errors = ts.load_task_metrics(tmp_path)
    assert errors == []
    assert m["ms_stock"]["date"] == "2026-11-24" and m["ms_stock"]["urls_checked"] == 900
    assert m["hm_stock"]["sku_stock_updated"] == 44


def test_task_metrics_all_corrupt_reports_error(tmp_path):
    (tmp_path / "stock_update_history.jsonl").write_text("garbage\n{bad\n", encoding="utf-8")
    m, errors = ts.load_task_metrics(tmp_path)
    assert "ms_stock" not in m
    assert errors and "stock_update_history.jsonl" in errors[0]


def test_task_metrics_large_file_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "HISTORY_TAIL_BYTES", 64)
    p = tmp_path / "stock_update_history.jsonl"
    _jsonl(p, MS_OLD, MS_REC)
    m, _ = ts.load_task_metrics(tmp_path)
    assert m["ms_stock"]["urls_checked"] == 900
    # tail holds only garbage -> whole file is scanned
    p.write_text(json.dumps(MS_REC) + "\n" + "x" * 500 + "\n", encoding="utf-8")
    m, errors = ts.load_task_metrics(tmp_path)
    assert m["ms_stock"]["urls_checked"] == 900 and errors == []


def test_task_metrics_old_price_records_give_none(tmp_path):
    _jsonl(tmp_path / "price_update_history.jsonl",
           {"date": "2026-11-23", "finished_at": "2026-11-23T05:40:00", "pushed_ok": 3800})
    m, errors = ts.load_task_metrics(tmp_path)
    assert errors == []
    pr = m["price"]
    assert _all_fields(pr)
    assert pr["sku_price_updated"] is None and pr["campaign_limit_updated"] is None
    assert pr["urls_checked"] is None and pr["prices_checked"] is None
    assert pr["update_date"] == "2026-11-23"
    assert pr["refresh_date"] is None and pr["refresh_finished_at"] is None


def test_task_metrics_comparison_null(tmp_path):
    rec = dict(MS_REC, comparison=None)
    _jsonl(tmp_path / "stock_update_history.jsonl", rec)
    m, _ = ts.load_task_metrics(tmp_path)
    assert m["ms_stock"]["sku_stock_updated"] is None
    assert m["ms_stock"]["urls_checked"] == 900
    no_cmp = {k: v for k, v in MS_REC.items() if k != "comparison"}
    _jsonl(tmp_path / "stock_update_history.jsonl", no_cmp)
    m, _ = ts.load_task_metrics(tmp_path)
    assert m["ms_stock"]["sku_stock_updated"] is None


def test_metric_key_for_task_case_insensitive():
    assert ts.metric_key_for_task("OzonSales_DailyStockUpdate") == "ms_stock"
    assert ts.metric_key_for_task("ozonsales_dailyhmstockupdate") == "hm_stock"
    assert ts.metric_key_for_task(r"\OZONSALES_DAILYPRICEUPDATE") == "price"
    assert ts.metric_key_for_task("MandsOzonDailyRun") is None
    assert ts.metric_key_for_task(None) is None


def test_collect_status_includes_task_metrics(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no subprocess allowed")

    monkeypatch.setattr(ts.subprocess, "run", boom)
    st = ts.collect_status(NOW, repo_dir=tmp_path, run_schtasks=False)
    assert st["task_metrics"] == {}
    _jsonl(tmp_path / "hm_stock_update_history.jsonl", HM_REC)
    (tmp_path / "stock_update_history.jsonl").write_text("junk\n", encoding="utf-8")
    st = ts.collect_status(NOW, repo_dir=tmp_path, run_schtasks=False)
    assert st["task_metrics"]["hm_stock"]["sku_stock_checked"] == 1800
    assert any("stock_update_history.jsonl" in e for e in st["errors"])
