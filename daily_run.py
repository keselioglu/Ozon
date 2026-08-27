"""
Unattended daily pipeline entry point, intended to be invoked by Windows Task
Scheduler once a day. Runs, in order:
  1. Priority-ordered category discovery (category_priority.py) — walks
     category_priority.csv from priority 1, queuing any not-yet-live products
     from the first category that has some, into product_urls.txt.
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
  7b. Sweep any in-stock, eligible product into its active Ozon campaign(s)
     (enroll_campaigns.py).
  8. Log to TASKS.md, commit & push.

Each step is a subprocess call to the existing, already-working scripts. This
script does not reimplement their logic — it only sequences them and stops on
the first step that fails in a way its own error handling doesn't already
cover, rather than retrying blindly.

All output is appended to daily_run_<date>.log (gitignored, like the other
*_log.txt state files) so a run can be inspected after the fact even though
Task Scheduler runs it headless.
"""
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_DIR = Path(__file__).resolve().parent
TODAY = datetime.now().strftime("%Y-%m-%d")
LOG_FILE = REPO_DIR / f"daily_run_{TODAY}.log"


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
    log(f"===== Daily run starting: {TODAY} =====")

    locked = check_for_file_locks()
    if locked:
        reason = (
            f"{', '.join(locked)} could not be opened for writing — probably open in "
            "Excel or another program on this machine. Close it and re-run."
        )
        log(f"BLOCKED: {reason}")
        _finalize_failed_run(reason)
        return

    counts = {"crawled": "?", "translated": 0, "translated_codes": [],
              "uploaded_ok": "?", "uploaded_failed": "?", "stock_ok": "?", "stock_failed": "?"}
    attention = []

    # 1. Priority-ordered category discovery — finds the first category (in
    # priority order) with a product not yet live on Ozon, and queues it into
    # product_urls.txt for the crawl step below to pick up. Not a hard failure
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

    # 3. Auto-translate any new products
    ok, translate_output = run_step("Auto-translate (auto_translate.py)", ["auto_translate.py"], timeout=1800)
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

    # 8. Update TASKS.md
    summary = (
        f"{TODAY}: {counts.get('discovery_summary', 'discovery status unknown')}, "
        f"translated {counts['translated']} "
        f"({', '.join(counts['translated_codes']) if counts['translated_codes'] else 'none'}), "
        f"{counts.get('upload_totals', 'upload status unknown')}, "
        f"{counts.get('stock_summary', 'stock sync status unknown')}, "
        f"{counts.get('live_stock_summary', 'live stock refresh status unknown')}, "
        f"{counts.get('campaign_summary', 'campaign enrollment status unknown')}"
    )
    update_tasks_md(summary)
    git_commit_and_push(f"Daily run log: {TODAY}")

    log("===== Daily run complete =====")
    log(summary)
    if attention:
        log("Needs human attention:")
        for a in attention:
            log(f"  {a}")


def _finalize_failed_run(reason, tasks_note=False, translated=0, translated_codes=None):
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


if __name__ == "__main__":
    main()
