"""
Unattended daily pipeline entry point, intended to be invoked by Windows Task
Scheduler once a day. Runs, in order:
  1. Priority-ordered category discovery (category_priority.py) — walks
     category_priority.csv from priority 1, queuing not-yet-live products
     into product_urls.txt until the day's plan (daily_plan.py, default 500
     new offers, per-category allocations) is covered.
  2. Crawl (crawler.py) — processes whatever's newly queued in product_urls.txt.
  2b. Re-check pages behind previously zero-stock-skipped sizes
     (recheck_stockouts.py), so a restocked size gets uploaded instead of
     staying permanently skipped.
  3. Auto-translate new products.
  4. Upload.
  5. Verify.
  6. Sync stock for today's crawled/uploaded rows (update_stocks.py).
  7. Refresh stock for all live M&S-sourced Ozon products with a known source
     URL, whether or not this pipeline crawled them today (refresh_live_stock.py).
  7a. Refresh today's M&S price for every live, source-URL-known product
     (refresh_prices.py) -- business instruction, 2026-09-02: "do we have a
     data of updated prices on marks&spencer daily? yes build it and update
     it daily". Tracking-only: does not push anything to Ozon.
  7b. Sweep any in-stock, healthy-margin (M&S cost <= 46% of enrollment
     price) product into its active Ozon campaign(s) (enroll_campaigns.py).
  7b.5. Re-check every currently-enrolled campaign product's ACTUAL live
     price (Elastic Boosting's current_boost drifts after enrollment) and
     remove/re-home anything that's drifted above the 46% margin threshold
     (rescan_campaign_margins.py).
  7b.6. Read-only catalog-wide margin report covering every live in-stock
     product, campaign or not (catalog_margin_report.py).
  7b.7. Read-only content-rating snapshot (content_rating.py), then the
     rating-driven content improvement job (improve_content.py): the
     lowest-rated (article, color) groups get their missing photos / video /
     rich content / size table / attributes as one merged, verified import
     per offer_id, capped by a share of the daily_update quota.
  7c. Detect (log-only for now) any campaign product Ozon added
     automatically rather than through 7b (check_auto_added_campaign_products.py).
  7d. Account snapshot (account_snapshot.py) -- read-only, offline rebuild of
     account_snapshot.json (the Ozon Sales Overview data) from local state
     files. Warning-only: a failure or timeout never fails the run.
  8. Log to TASKS.md, commit & push.
  9. Post the run summary as a comment on GitHub issue #13 (business
     instruction, 2026-09-02: "i just need reminders and daily reports to my
     phone while i am away") -- the user has no push-notification channel
     set up (PushNotification requires Remote Control pairing, not done),
     but is signed into GitHub mobile, so a comment there arrives as a
     native phone notification. Not a hard failure if this step errors --
     the run itself already succeeded/failed independently of reporting it.

Each step is a subprocess call to the existing, already-working scripts. This
script does not reimplement their logic — it only sequences them and stops on
the first step that fails in a way its own error handling doesn't already
cover, rather than retrying blindly.

All output is appended to daily_run_<date>.log (gitignored, like the other
*_log.txt state files) so a run can be inspected after the fact even though
Task Scheduler runs it headless.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_DIR = Path(__file__).resolve().parent
TODAY = datetime.now().strftime("%Y-%m-%d")
LOG_FILE = REPO_DIR / f"daily_run_{TODAY}.log"
# Machine-readable run status for the 10am report (gitignored, overwritten
# atomically by write_run_status()).
STATUS_FILE = REPO_DIR / "daily_run_status.json"
# One {name, script, status, seconds} dict per run_step() call this run.
STEP_RESULTS = []
RUN_STARTED_AT = None

REPORT_REPO = "keselioglu/Ozon"
REPORT_ISSUE = 13
GH_EXE = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"


def post_github_report(body):
    """Posts `body` as a comment on the daily-reports tracking issue, from the
    Tugberk-AI collaborator account, mentioning @keselioglu so it actually
    triggers a phone notification (confirmed live, 2026-09-02: GitHub does
    not notify on self-mentions, so this must be posted by a different
    account than the recipient's own). Best-effort -- logs and swallows any
    failure rather than affecting the run's outcome."""
    body = f"@keselioglu\n\n{body}"
    try:
        result = subprocess.run(
            [GH_EXE, "issue", "comment", str(REPORT_ISSUE), "--repo", REPORT_REPO, "--body", body],
            cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if result.returncode != 0:
            log(f"Could not post GitHub report: {result.stderr.strip()}")
        else:
            log("Posted run summary to GitHub issue #13.")
    except Exception as e:
        log(f"Could not post GitHub report: {e}")


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# Files the pipeline writes to that a person could plausibly have open in Excel
# (Windows locks a file against other writers while it's open there) — checked
# before the run starts so a lock fails fast with a clear message instead of a
# crawler traceback halfway through discovery.
LOCK_CHECK_FILES = ["products.csv", "category_priority.csv", "legacy_product_urls.csv"]


def check_for_file_locks():
    """Returns a list of filenames that exist and can't currently be opened for
    writing (most likely because they're open in Excel or another program)."""
    locked = []
    for name in LOCK_CHECK_FILES:
        path = REPO_DIR / name
        if not path.exists():
            continue
        try:
            with open(path, "a", encoding="utf-8"):
                pass
        except PermissionError:
            locked.append(name)
    return locked


def run_step(name, args, timeout=1800):
    """Runs one pipeline step as a subprocess, streaming+logging its output.
    Returns (success, combined_output)."""
    log(f"--- {name} ---")
    started = time.monotonic()

    def record(status):
        STEP_RESULTS.append({"name": name, "script": args[0] if args else None,
                             "status": status,
                             "seconds": round(time.monotonic() - started, 1)})

    try:
        result = subprocess.run(
            [sys.executable] + args,
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        log(f"{name} TIMED OUT after {timeout}s")
        record("timeout")
        return False, str(e)

    output = (result.stdout or "") + (result.stderr or "")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(output + "\n")
    print(output)

    if result.returncode != 0:
        log(f"{name} FAILED (exit code {result.returncode})")
        record("failed")
        return False, output

    log(f"{name} completed.")
    record("ok")
    return True, output


def run_git(args, check=True):
    result = subprocess.run(
        ["git"] + args, cwd=REPO_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def git_commit_and_push(message):
    """Stages every tracked change and commits+pushes if there's anything to commit.
    Returns True if a commit was made (and push attempted), False if nothing changed."""
    run_git(["add", "-A"])
    status = run_git(["status", "--porcelain"])
    if not status.stdout.strip():
        log("Nothing to commit.")
        return False

    run_git(["commit", "-m", message])
    push = run_git(["push"], check=False)
    if push.returncode != 0:
        log(f"WARNING: git push failed: {push.stderr.strip()}")
        log("Commit exists locally but was not pushed — needs manual attention.")
        return False
    log("Committed and pushed.")
    return True


def plan_progress():
    """(created, plan_total) from new_items_submitted.json + daily_plan.json
    (both local files, no API call). created is 0 if the log is missing or
    from another day. Raises if the plan itself can't be resolved."""
    import daily_plan
    plan_total = daily_plan.plan_for_date(daily_plan.load_plan(REPO_DIR / daily_plan.PLAN_FILE), None)["total"]
    created = 0
    try:
        with open(REPO_DIR / daily_plan.NEW_ITEMS_LOG, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("date") == daily_plan.utc_today():
            created = len(data.get("new_offer_ids") or [])
    except (OSError, ValueError):
        pass
    return created, plan_total


def plan_progress_line():
    """'plan N/500 created' (see plan_progress()); a short 'unknown' note if
    unreadable."""
    try:
        created, plan_total = plan_progress()
        return f"plan {created}/{plan_total} created"
    except Exception as e:
        return f"plan status unknown ({e})"


def _done_line(output):
    """The first line of a step's output that starts with "Done.", or ""."""
    return next((l.strip() for l in (output or "").splitlines() if l.startswith("Done.")), "")


def content_summary(rating_done, improve_done):
    """Short summary-line fragment from the 'Done.' lines of content_rating.py
    ("Done. N rated, avg X.X, M below 80.") and improve_content.py
    ("Done. G groups / O offers improved ..."), e.g.
    "content: avg 63.2, 120 groups improved". A missing / unparseable part
    reads "status unknown"."""
    m = re.search(r"\bavg ([0-9]+(?:\.[0-9]+)?|n/a)\b", rating_done or "")
    avg = m.group(1) if m else None
    m = re.match(r"Done\. (\d+) groups\b", improve_done or "")
    groups = m.group(1) if m else None
    if avg is None and groups is None:
        return "content status unknown"
    return (f"content: {'avg ' + avg if avg is not None else 'rating status unknown'}, "
            f"{groups + ' groups improved' if groups is not None else 'improvement status unknown'}")


def write_run_status(state, summary=None, attention=None, counts=None):
    """Writes STATUS_FILE atomically (tmp file + os.replace) for the 10am
    report. state: 'running' | 'completed' | 'failed' | 'blocked'. Never
    raises: a status-write failure is logged and the run carries on."""
    try:
        counts = counts or {}
        try:
            created, plan_total = plan_progress()
        except Exception:
            created, plan_total = None, None
        status = {
            "date": TODAY,
            "state": state,
            "started_at": RUN_STARTED_AT,
            "finished_at": None if state == "running" else datetime.now().isoformat(timespec="seconds"),
            "steps": list(STEP_RESULTS),
            "summary": summary,
            "attention": list(attention or []),
            "created_today": created,
            "plan_total": plan_total,
            "content_summary": counts.get("content_summary"),
            "upload_totals": counts.get("upload_totals"),
        }
        path = Path(STATUS_FILE)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        try:
            log(f"WARNING: could not write {STATUS_FILE}: {e}")
        except Exception:
            pass


def update_tasks_md(summary_line):
    tasks_path = REPO_DIR / "TASKS.md"
    content = tasks_path.read_text(encoding="utf-8")

    section_header = "## Daily Run Log"
    entry = f"- {summary_line}\n"

    if section_header in content:
        marker = f"{section_header}\n"
        idx = content.index(marker) + len(marker)
        content = content[:idx] + entry + content[idx:]
    else:
        done_header = "## Done"
        block = f"{section_header}\n\n{entry}\n---\n\n"
        content = content.replace(done_header, block + done_header, 1) if done_header in content else content + f"\n---\n\n{section_header}\n\n{entry}"

    tasks_path.write_text(content, encoding="utf-8")


def main():
    global RUN_STARTED_AT
    RUN_STARTED_AT = datetime.now().isoformat(timespec="seconds")
    STEP_RESULTS.clear()
    log(f"===== Daily run starting: {TODAY} =====")
    write_run_status("running")

    locked = check_for_file_locks()
    if locked:
        reason = (
            f"{', '.join(locked)} could not be opened for writing — probably open in "
            "Excel or another program on this machine. Close it and re-run."
        )
        log(f"BLOCKED: {reason}")
        _finalize_failed_run(reason, state="blocked")
        return

    # Created-offer history for the planner (category_inventory.py): local
    # files only, no API. Before the upload, new_items_submitted.json still
    # holds YESTERDAY's final list (10:15 quota-fill included). Non-fatal.
    run_step("Created history (category_inventory.py --record-history)",
             ["category_inventory.py", "--record-history"], timeout=300)

    counts = {"crawled": "?", "translated": 0, "translated_codes": [],
              "uploaded_ok": "?", "uploaded_failed": "?", "stock_ok": "?", "stock_failed": "?"}
    attention = []

    # 1. Priority-ordered category discovery — queues not-yet-live products,
    # in priority order, into product_urls.txt until the day's plan is
    # covered, for the crawl step below to pick up. Not a hard failure
    # if this step errors — crawler.py can still process anything already
    # queued from a previous run, so log and continue rather than stopping.
    ok, discovery_output = run_step("Category discovery (category_priority.py)", ["category_priority.py"], timeout=3600)
    if not ok:
        log("Category discovery failed — continuing to crawl step with whatever's already queued.")
        attention.append("Category discovery step failed — see log for details.")
    elif "Summary:" in discovery_output:
        summary_line = next((l for l in discovery_output.splitlines() if l.startswith("Summary:")), "")
        counts["discovery_summary"] = summary_line

    # 2. Crawl
    ok, _ = run_step("Crawl (crawler.py)", ["crawler.py"], timeout=3600)
    if not ok:
        log("Crawl step failed — stopping run. Not proceeding to translate/upload with stale data.")
        _finalize_failed_run("Crawl step failed — see log for details.")
        return

    # 2b. Re-check pages behind previously zero-stock-skipped sizes, so a
    # restocked size gets uploaded on this run instead of staying permanently
    # skipped (crawler.py never revisits an already-processed URL on its own
    # -- see recheck_stockouts.py's docstring, GitHub issue #9). Not a hard
    # failure -- upload still proceeds with whatever products.csv already has.
    ok, recheck_output = run_step("Stockout re-check (recheck_stockouts.py)", ["recheck_stockouts.py"], timeout=3600)
    if not ok:
        attention.append("Stockout re-check step failed — see log for details.")
    elif "now show real stock" in recheck_output:
        restock_line = next((l for l in recheck_output.splitlines() if "now show real stock" in l), "")
        counts["restock_summary"] = restock_line

    # 3. Auto-translate any new products. Timeout raised from 30min to 3h,
    # 2026-09-05: Ozon's daily_create quota jumped from 250 to 2000
    # (account-level change) and auto_translate.py's sequential
    # one-Claude-call-per-product loop timed out trying to translate a
    # 1,118-product discovery batch under the old 30min limit, failing
    # the whole day's run. auto_translate.py itself is unchanged --
    # translation just genuinely takes longer at 8x the daily volume.
    ok, translate_output = run_step("Auto-translate (auto_translate.py)", ["auto_translate.py"], timeout=10800)
    if not ok:
        log("Translate step failed — stopping run rather than uploading with an inconsistent translations file.")
        _finalize_failed_run("Auto-translate step failed — see log for details.")
        return
    for line in translate_output.splitlines():
        if line.startswith("OK   "):
            code = line.split()[1].rstrip(":")
            counts["translated_codes"].append(code)
        elif line.startswith("SKIP "):
            attention.append(line.strip())
    counts["translated"] = len(counts["translated_codes"])

    # Commit + push translations now, before upload, so they're preserved even if
    # a later step fails (matches the routine's original design intent).
    if counts["translated"]:
        git_commit_and_push(
            f"Add translations for {counts['translated']} new product(s): "
            + ", ".join(counts["translated_codes"])
        )

    # 4. Upload (quota capping/dedup/batching handled inside upload_to_ozon.py)
    ok, _ = run_step("Upload (upload_to_ozon.py)", ["upload_to_ozon.py"], timeout=1800)
    if not ok:
        log("Upload step failed — stopping run before stock sync.")
        _finalize_failed_run("Upload step failed — see log for details.", tasks_note=True,
                              translated=counts["translated"], translated_codes=counts["translated_codes"])
        return

    # 5. Verify — Ozon processes async, give it a moment before checking
    import time
    time.sleep(15)
    ok, verify_output = run_step("Verify (check_upload_status.py)", ["check_upload_status.py"], timeout=600)
    if "Totals:" in verify_output:
        totals_line = next(l for l in verify_output.splitlines() if l.startswith("Totals:"))
        counts["upload_totals"] = totals_line

    # 6. Sync stock for today's crawled/uploaded rows
    ok, stock_output = run_step("Stock sync (update_stocks.py)", ["update_stocks.py"], timeout=1800)
    if "Done." in stock_output:
        done_line = next((l for l in stock_output.splitlines() if l.startswith("Done.")), "")
        counts["stock_summary"] = done_line

    # 7. Refresh stock for all live M&S-sourced products with a known source
    # URL (covers products this pipeline didn't crawl today, or ever). Not a
    # hard failure — log and continue to the TASKS.md update either way.
    ok, live_stock_output = run_step("Live stock refresh (refresh_live_stock.py)", ["refresh_live_stock.py"], timeout=3600)
    if not ok:
        attention.append("Live stock refresh step failed — see log for details.")
    elif "Done." in live_stock_output:
        done_line = next((l for l in live_stock_output.splitlines() if l.startswith("Done.")), "")
        counts["live_stock_summary"] = done_line

    # 7a. Refresh today's M&S price for every live, source-URL-known product
    # (refresh_prices.py, business instruction 2026-09-02). Tracking-only --
    # does not push anything to Ozon. Not a hard failure either way.
    ok, price_output = run_step("Price refresh (refresh_prices.py)", ["refresh_prices.py"], timeout=3600)
    if not ok:
        attention.append("Price refresh step failed — see log for details.")
    elif "Done." in price_output:
        done_line = next((l for l in price_output.splitlines() if l.startswith("Done.")), "")
        counts["price_summary"] = done_line
        if ", 0 changed," not in done_line:
            attention.append(f"M&S price changes detected today: {done_line}")

    # 7b. Sweep any in-stock, eligible product into its active Ozon
    # campaign(s) (enroll_campaigns.py, GitHub issue #10) -- runs after
    # stock refresh so a product that just restocked is picked up the same
    # day, not a day late. Not a hard failure either way.
    ok, campaign_output = run_step("Campaign enrollment (enroll_campaigns.py)", ["enroll_campaigns.py"], timeout=1800)
    if not ok:
        attention.append("Campaign enrollment step failed — see log for details.")
    elif "Done." in campaign_output:
        done_line = next((l for l in campaign_output.splitlines() if l.startswith("Done.")), "")
        counts["campaign_summary"] = done_line

    # 7b.5. Re-check every CURRENTLY ENROLLED campaign product's actual live
    # price (not just its enrollment-time reference) against today's M&S
    # cost, since Elastic Boosting's current_boost drifts continuously
    # after enrollment (business instruction, 2026-09-02, after finding 288
    # products had drifted past the 46% threshold live). Removes anything
    # that's drifted thin, then tries to re-home each removal into another
    # qualifying campaign before giving up on it. Not a hard failure either way.
    ok, rescan_output = run_step(
        "Campaign margin re-scan (rescan_campaign_margins.py)",
        ["rescan_campaign_margins.py"], timeout=1800)
    if not ok:
        attention.append("Campaign margin re-scan step failed — see log for details.")
    elif "Done." in rescan_output:
        done_line = next((l for l in rescan_output.splitlines() if l.startswith("Done.")), "")
        counts["margin_rescan_summary"] = done_line
        if not done_line.startswith("Done. 0 removed"):
            attention.append(f"Campaign margin drift found and corrected: {done_line}")

    # 7b.6. Read-only catalog-wide margin visibility report -- covers every
    # live, in-stock product regardless of campaign enrollment (business
    # instruction, 2026-09-02: "check all live products with stock if the
    # cost is higher than 46% even not in a campaign and report"). Nothing
    # is changed here -- this pipeline doesn't push price changes to Ozon.
    ok, catalog_output = run_step(
        "Catalog-wide margin report (catalog_margin_report.py)",
        ["catalog_margin_report.py"], timeout=3600)
    if not ok:
        attention.append("Catalog margin report step failed — see log for details.")
    elif "Done." in catalog_output:
        done_line = next((l for l in catalog_output.splitlines() if l.startswith("Done.")), "")
        counts["catalog_margin_summary"] = done_line
        if not done_line.rstrip(".").endswith("0 flagged (ratio > 46%)"):
            attention.append(f"Catalog-wide margin risk found (report only, not acted on): {done_line}")

    # 7b.7. Content rating snapshot (read-only) + rating-driven content
    # improvement (task #39). Neither step fails the run: a failure or
    # timeout just leaves "status unknown" in the summary.
    ok, rating_output = run_step(
        "Content rating (content_rating.py)",
        ["content_rating.py"], timeout=1800)
    rating_done = _done_line(rating_output) if ok else ""
    if not ok:
        attention.append("Content rating step failed — see log for details.")
    ok, improve_output = run_step(
        "Content improvement (improve_content.py)",
        ["improve_content.py"], timeout=3600)
    improve_done = _done_line(improve_output) if ok else ""
    if not ok:
        attention.append("Content improvement step failed — see log for details.")
    counts["content_summary"] = content_summary(rating_done, improve_done)

    # 7c. Detect (log-only, does not remove yet -- see module docstring)
    # any campaign-enrolled product Ozon added automatically rather than
    # through our own enroll_campaigns.py (business instruction,
    # 2026-09-02: "if any product automatically is found remove these from
    # the campaign and log it"). Not a hard failure either way.
    ok, auto_added_output = run_step(
        "Auto-added campaign product check (check_auto_added_campaign_products.py)",
        ["check_auto_added_campaign_products.py"], timeout=900)
    if not ok:
        attention.append("Auto-added campaign product check failed — see log for details.")
    elif "Done." in auto_added_output:
        done_line = next((l for l in auto_added_output.splitlines() if l.startswith("Done.")), "")
        counts["auto_added_check_summary"] = done_line
        if "ALERT:" in auto_added_output:
            attention.append(f"Auto-added campaign products detected (not yet removed, see module docstring): {done_line}")

    # Today's created-offer totals so far (local only, non-fatal).
    run_step("Created history (category_inventory.py --record-history)",
             ["category_inventory.py", "--record-history"], timeout=300)

    # 7d. Account snapshot -- read-only and offline (no Ozon calls): reads
    # catalog_status.json / catalog_live_offer_ids.json /
    # warehouse_assignments.json and writes account_snapshot.json for the
    # Ozon Sales Overview. Warning-only: never fails the run.
    ok, _ = run_step("Account snapshot (account_snapshot.py)", ["account_snapshot.py"], timeout=600)
    if not ok:
        attention.append("Account snapshot step failed — see log for details "
                         "(warning only; the Ozon Sales Overview may show stale numbers).")

    # 8. Update TASKS.md
    summary = (
        f"{TODAY}: {counts.get('discovery_summary', 'discovery status unknown')}, "
        f"translated {counts['translated']} "
        f"({', '.join(counts['translated_codes']) if counts['translated_codes'] else 'none'}), "
        f"{counts.get('upload_totals', 'upload status unknown')}, "
        f"{plan_progress_line()}, "
        f"{counts.get('stock_summary', 'stock sync status unknown')}, "
        f"{counts.get('live_stock_summary', 'live stock refresh status unknown')}, "
        f"{counts.get('price_summary', 'price refresh status unknown')}, "
        f"{counts.get('campaign_summary', 'campaign enrollment status unknown')}, "
        f"{counts.get('margin_rescan_summary', 'margin re-scan status unknown')}, "
        f"{counts.get('catalog_margin_summary', 'catalog margin report status unknown')}, "
        f"{counts.get('content_summary', 'content status unknown')}"
    )
    update_tasks_md(summary)
    git_commit_and_push(f"Daily run log: {TODAY}")

    log("===== Daily run complete =====")
    log(summary)
    if attention:
        log("Needs human attention:")
        for a in attention:
            log(f"  {a}")

    report = f"### Daily run: {TODAY}\n\n{summary}"
    if attention:
        report += "\n\n**Needs attention:**\n" + "\n".join(f"- {a}" for a in attention)
    post_github_report(report)
    write_run_status("completed", summary, attention, counts)


def _finalize_failed_run(reason, tasks_note=False, translated=0, translated_codes=None,
                         state="failed"):
    """Records a failed run in TASKS.md and commits whatever partial progress exists,
    so tomorrow's run (and the human) has visibility into what happened."""
    summary = f"{TODAY}: FAILED — {reason}"
    if tasks_note:
        summary += f" (translated {translated} before failing: {', '.join(translated_codes or [])})"
    try:
        update_tasks_md(summary)
        git_commit_and_push(f"Daily run log: {TODAY} (failed)")
    except Exception as e:
        log(f"Could not even log the failure to TASKS.md: {e}")
    log(summary)
    post_github_report(f"### Daily run: {TODAY}\n\n**FAILED**\n\n{summary}")
    write_run_status(state, summary, [reason])


if __name__ == "__main__":
    main()
