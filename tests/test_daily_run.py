import time

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
