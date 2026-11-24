import time
import pytest

import daily_run


def test_content_summary_parses_both_done_lines():
    rating = "Done. 4100 rated, avg 63.2, 2900 below 80."
    improve = ("Done. 120 groups / 610 offers improved (photos 500, videos 100, rich 90, "
               "size-table 40, attrs 300), 2 failed, avg rating 55.0 -> 61.5 (yesterday's batch).")
    assert daily_run.content_summary(rating, improve) == "content: avg 63.2, 120 groups improved"


def test_content_summary_partial_and_missing():
    assert daily_run.content_summary("", "") == "content status unknown"
    assert (daily_run.content_summary("Done. 10 rated, avg 70.0, 3 below 80.", "")
            == "content: avg 70.0, improvement status unknown")
    assert (daily_run.content_summary("", "Done. 0 groups / 0 offers improved (...)")
            == "content: rating status unknown, 0 groups improved")
    assert (daily_run.content_summary("Done. 0 rated, avg n/a, 0 below 80.", "")
            == "content: avg n/a, improvement status unknown")


def test_done_line():
    assert daily_run._done_line("a\nDone. 5 x.\nDone. 6 y.") == "Done. 5 x."
    assert daily_run._done_line("") == ""
    assert daily_run._done_line(None) == ""


def _patch_run(monkeypatch, failing):
    calls, summaries, reports, commits = [], [], [], []

    def fake_run_step(name, args, timeout=1800):
        calls.append((name, args, timeout))
        if args[0] in failing:
            return False, "boom"
        if args[0] == "check_upload_status.py":
            return True, "Totals: 1 succeeded/moderating, 0 failed, 0 pending."
        return True, "Done. ok."

    monkeypatch.setattr(daily_run, "run_step", fake_run_step)
    monkeypatch.setattr(daily_run, "check_for_file_locks", lambda: [])
    monkeypatch.setattr(daily_run, "log", lambda msg: None)
    monkeypatch.setattr(daily_run, "update_tasks_md", lambda s: summaries.append(s))
    monkeypatch.setattr(daily_run, "git_commit_and_push", lambda m: commits.append(m) or True)
    monkeypatch.setattr(daily_run, "post_github_report", lambda body: reports.append(body))
    monkeypatch.setattr(daily_run, "plan_progress_line", lambda: "plan 0/500 created")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return calls, summaries, reports, commits


def test_content_steps_order_and_timeouts(monkeypatch):
    calls, summaries, _, _ = _patch_run(monkeypatch, failing=())
    daily_run.main()
    scripts = [c[1][0] for c in calls]
    i_cat = scripts.index("catalog_margin_report.py")
    i_rating = scripts.index("content_rating.py")
    i_improve = scripts.index("improve_content.py")
    i_auto = scripts.index("check_auto_added_campaign_products.py")
    assert i_cat < i_rating < i_improve < i_auto
    assert calls[i_rating][2] == 1800
    assert calls[i_improve][2] == 3600
    assert len(summaries) == 1


def test_content_step_failures_do_not_stop_run(monkeypatch):
    calls, summaries, reports, commits = _patch_run(
        monkeypatch, failing=("content_rating.py", "improve_content.py"))
    daily_run.main()
    scripts = [c[1][0] for c in calls]
    assert "check_auto_added_campaign_products.py" in scripts
    assert scripts.index("check_auto_added_campaign_products.py") > scripts.index("improve_content.py")
    assert len(summaries) == 1
    assert "FAILED" not in summaries[0]
    assert summaries[0].endswith("content status unknown")
    assert f"Daily run log: {daily_run.TODAY}" in commits
    assert len(reports) == 1
    assert "Content improvement step failed" in reports[0]
    assert "Content rating step failed" in reports[0]


# --- daily_run_status.json (task #50) ---

import json
import subprocess


def test_run_step_records_ok_failed_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_run, "log", lambda msg: None)
    monkeypatch.setattr(daily_run, "LOG_FILE", tmp_path / "run.log")
    monkeypatch.setattr(daily_run, "STEP_RESULTS", [])
    outcomes = iter([0, 1, "timeout"])

    def fake_run(cmd, **kw):
        rc = next(outcomes)
        if rc == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, rc, stdout="out", stderr="")

    monkeypatch.setattr(daily_run.subprocess, "run", fake_run)
    assert daily_run.run_step("A", ["a.py"])[0] is True
    assert daily_run.run_step("B", ["b.py", "--x"])[0] is False
    ok, out = daily_run.run_step("C", ["c.py"], timeout=5)
    assert ok is False and isinstance(out, str)
    steps = daily_run.STEP_RESULTS
    assert [(s["name"], s["script"], s["status"]) for s in steps] == [
        ("A", "a.py", "ok"), ("B", "b.py", "failed"), ("C", "c.py", "timeout")]
    assert all(isinstance(s["seconds"], float) for s in steps)


def _read_status(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_main_writes_completed_status(monkeypatch, tmp_path):
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    monkeypatch.setattr(daily_run, "plan_progress", lambda: (12, 500))
    _, summaries, _, _ = _patch_run(monkeypatch, failing=())
    daily_run.main()
    st = _read_status(status_file)
    assert st["date"] == daily_run.TODAY
    assert st["state"] == "completed"
    assert st["summary"] == summaries[0]
    assert isinstance(st["attention"], list)
    assert not any("step failed" in a for a in st["attention"])
    assert isinstance(st["steps"], list)
    assert st["created_today"] == 12 and st["plan_total"] == 500
    assert st["started_at"] and st["finished_at"]
    assert st["upload_totals"].startswith("Totals:")
    assert st["content_summary"] == "content status unknown"
    assert not (tmp_path / "daily_run_status.json.tmp").exists()


def test_main_content_failures_completed_with_attention(monkeypatch, tmp_path):
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    _patch_run(monkeypatch, failing=("content_rating.py", "improve_content.py"))
    daily_run.main()
    st = _read_status(status_file)
    assert st["state"] == "completed"
    assert any("Content rating step failed" in a for a in st["attention"])
    assert any("Content improvement step failed" in a for a in st["attention"])


def test_main_crawl_failure_writes_failed(monkeypatch, tmp_path):
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    _patch_run(monkeypatch, failing=("crawler.py",))
    daily_run.main()
    st = _read_status(status_file)
    assert st["state"] == "failed"
    assert "FAILED" in st["summary"]


def test_finalize_failed_run_states(monkeypatch, tmp_path):
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    _patch_run(monkeypatch, failing=())
    daily_run._finalize_failed_run("Crawl step failed")
    st = _read_status(status_file)
    assert st["state"] == "failed" and st["attention"] == ["Crawl step failed"]
    daily_run._finalize_failed_run("products.csv locked", state="blocked")
    assert _read_status(status_file)["state"] == "blocked"


def test_main_blocked_by_file_lock(monkeypatch, tmp_path):
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    _patch_run(monkeypatch, failing=())
    monkeypatch.setattr(daily_run, "check_for_file_locks", lambda: ["products.csv"])
    daily_run.main()
    assert _read_status(status_file)["state"] == "blocked"


def test_status_write_failure_never_raises(monkeypatch, tmp_path):
    logged = []
    monkeypatch.setattr(daily_run, "log", logged.append)
    monkeypatch.setattr(daily_run, "STATUS_FILE", tmp_path / "missing_dir" / "s.json")
    daily_run.write_run_status("running")
    assert any("could not write" in m for m in logged)


# --- account snapshot step (task #67) ---

def test_account_snapshot_step_in_list(monkeypatch):
    calls, summaries, _, _ = _patch_run(monkeypatch, failing=())
    daily_run.main()
    scripts = [c[1][0] for c in calls]
    assert "account_snapshot.py" in scripts
    i_snap = scripts.index("account_snapshot.py")
    assert calls[i_snap][2] == 600
    for earlier in ("refresh_live_stock.py", "update_stocks.py",
                    "check_auto_added_campaign_products.py"):
        assert scripts.index(earlier) < i_snap
    assert len(summaries) == 1


def test_account_snapshot_failure_does_not_fail_run(monkeypatch, tmp_path):
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    _, summaries, reports, commits = _patch_run(monkeypatch, failing=("account_snapshot.py",))
    daily_run.main()
    assert len(summaries) == 1
    assert "FAILED" not in summaries[0]
    assert f"Daily run log: {daily_run.TODAY}" in commits
    assert len(reports) == 1
    assert "Account snapshot step failed" in reports[0]
    st = _read_status(status_file)
    assert st["state"] == "completed"
    assert any("Account snapshot step failed" in a for a in st["attention"])


def _patch_subprocess_run(monkeypatch, tmp_path, snapshot_outcome):
    summaries, reports, commits = [], [], []
    status_file = tmp_path / "daily_run_status.json"
    monkeypatch.setattr(daily_run, "LOG_FILE", tmp_path / "run.log")
    monkeypatch.setattr(daily_run, "STATUS_FILE", status_file)
    monkeypatch.setattr(daily_run, "STEP_RESULTS", [])
    monkeypatch.setattr(daily_run, "check_for_file_locks", lambda: [])
    monkeypatch.setattr(daily_run, "log", lambda msg: None)
    monkeypatch.setattr(daily_run, "update_tasks_md", lambda s: summaries.append(s))
    monkeypatch.setattr(daily_run, "git_commit_and_push", lambda m: commits.append(m) or True)
    monkeypatch.setattr(daily_run, "post_github_report", lambda body: reports.append(body))
    monkeypatch.setattr(daily_run, "plan_progress_line", lambda: "plan 0/500 created")
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def fake_run(cmd, **kw):
        if "account_snapshot.py" in cmd:
            if snapshot_outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, 1, stdout="boom", stderr="")
        if "check_upload_status.py" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Totals: 1 succeeded/moderating, 0 failed, 0 pending.", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="Done. ok.", stderr="")

    monkeypatch.setattr(daily_run.subprocess, "run", fake_run)
    return status_file, summaries, reports, commits


@pytest.mark.parametrize("outcome,expected", [("timeout", "timeout"), ("rc1", "failed")])
def test_account_snapshot_timeout_or_error_still_completes(monkeypatch, tmp_path, outcome, expected):
    status_file, summaries, reports, commits = _patch_subprocess_run(monkeypatch, tmp_path, outcome)
    daily_run.main()
    snap = [s for s in daily_run.STEP_RESULTS if s["script"] == "account_snapshot.py"]
    assert len(snap) == 1 and snap[0]["status"] == expected
    assert len(summaries) == 1 and "FAILED" not in summaries[0]
    assert f"Daily run log: {daily_run.TODAY}" in commits
    assert len(reports) == 1 and "Account snapshot step failed" in reports[0]
    st = _read_status(status_file)
    assert st["state"] == "completed"
    assert any(s["script"] == "account_snapshot.py" and s["status"] == expected for s in st["steps"])
