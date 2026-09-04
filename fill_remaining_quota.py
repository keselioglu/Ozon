"""
Post-10am quota fill (business instruction, 2026-09-04): "arrange the
daily tasks again that there will be no remaining limit. if there is
limit in the daily report go and add that number of products after
10.am". Runs right after daily_report.py, reads the same live
daily_create quota it reported, and if any is left, runs the
discover -> crawl -> translate -> upload sequence (same steps
daily_run.py itself uses) targeted at exactly that remaining amount.

Distinct from the existing 5am "quota top-up" step (run_quota_topup.bat,
upload_to_ozon.py alone) -- that one only re-submits whatever's already
queued in product_urls.txt/deferred_items.json, it does not go looking
for NEW products. By 10am the queue is typically already exhausted (the
4am/5am runs already consumed it), so simply re-running upload_to_ozon.py
again finds nothing new to submit even when quota remains. This script
runs category_priority.py first (targeted at the exact remaining quota,
per the fix already in that script -- see its own docstring) so there's
something new to crawl/translate/upload before checking quota again.

Scheduled via Windows Task Scheduler (MandsOzonQuotaFill, 10:15am daily --
15 minutes after MandsOzonDailyReport, so the quota figure it acts on is
current) as its own script, separate from daily_run.py.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from daily_run import GH_EXE, REPORT_ISSUE, REPORT_REPO  # reuse the same GitHub report channel

REPO_DIR = Path(__file__).resolve().parent
TODAY = datetime.now().strftime("%Y-%m-%d")
LOG_FILE = REPO_DIR / f"daily_run_{TODAY}.log"


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def post_github_report(body):
    body = f"@keselioglu\n\n{body}"
    try:
        result = subprocess.run(
            [GH_EXE, "issue", "comment", str(REPORT_ISSUE), "--repo", REPORT_REPO, "--body", body],
            cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if result.returncode != 0:
            log(f"Could not post GitHub report: {result.stderr.strip()}")
        else:
            log("Posted quota-fill summary to GitHub issue #13.")
    except Exception as e:
        log(f"Could not post GitHub report: {e}")


def run_step(name, args, timeout=3600):
    log(f"--- {name} ---")
    try:
        result = subprocess.run(
            [sys.executable] + args,
            cwd=REPO_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        log(f"{name} TIMED OUT after {timeout}s")
        return False, str(e)

    output = (result.stdout or "") + (result.stderr or "")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(output + "\n")
    print(output)

    if result.returncode != 0:
        log(f"{name} FAILED (exit code {result.returncode})")
        return False, output

    log(f"{name} completed.")
    return True, output


def main():
    from upload_to_ozon import check_quota

    quota = check_quota()
    if not quota:
        return log("Could not read quota — skipping quota-fill step.")

    dc = quota.get("daily_create", {})
    remaining = dc.get("limit", 0) - dc.get("usage", 0)
    log(f"Daily create quota: {dc.get('usage')}/{dc.get('limit')} used, {remaining} remaining.")

    if remaining <= 0:
        return log("No remaining quota — nothing to fill.")

    log(f"===== Quota-fill run starting: {TODAY} ({remaining} remaining) =====")

    ok, discovery_output = run_step("Category discovery (category_priority.py)", ["category_priority.py"])
    if not ok:
        log("Category discovery failed — nothing new queued, stopping.")
        post_github_report(f"### Quota-fill run: {TODAY}\n\nCategory discovery failed — see log for details.")
        return

    ok, _ = run_step("Crawl (crawler.py)", ["crawler.py"])
    if not ok:
        log("Crawl failed — stopping before translate/upload.")
        post_github_report(f"### Quota-fill run: {TODAY}\n\nCrawl step failed — see log for details.")
        return

    ok, translate_output = run_step("Auto-translate (auto_translate.py)", ["auto_translate.py"])
    translated = 0
    if ok:
        translated = sum(1 for line in translate_output.splitlines() if line.startswith("OK   "))

    ok, upload_output = run_step("Upload (upload_to_ozon.py)", ["upload_to_ozon.py"])
    upload_summary = "upload status unknown"
    if ok and "Done." in upload_output:
        upload_summary = next((l for l in upload_output.splitlines() if l.startswith("Done.")), upload_summary)

    quota_after = check_quota()
    remaining_after = None
    if quota_after:
        dc_after = quota_after.get("daily_create", {})
        remaining_after = dc_after.get("limit", 0) - dc_after.get("usage", 0)

    summary = (
        f"### Quota-fill run: {TODAY}\n\n"
        f"Started with {remaining} remaining, translated {translated} new product(s), "
        f"{upload_summary}\n\n"
        f"Quota remaining after this run: {remaining_after if remaining_after is not None else 'unknown'}"
    )
    log(summary)
    post_github_report(summary)


if __name__ == "__main__":
    main()
