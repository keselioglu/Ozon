"""Offline tests for task_status.py (task #54). No real schtasks call."""
import json
import subprocess
from datetime import datetime

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
