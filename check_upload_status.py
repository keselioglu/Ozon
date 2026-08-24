"""
Checks the status of all task_ids logged by upload_to_ozon.py and reports
which products actually succeeded vs failed, with Ozon's own error messages.
"""
import json
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call

TASK_LOG = "upload_tasks.jsonl"


def check_task(task_id):
    return call("/v1/product/import/info", {"task_id": task_id})


def main():
    try:
        with open(TASK_LOG, "r", encoding="utf-8") as f:
            tasks = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return print(f"{TASK_LOG} not found. Run upload_to_ozon.py first.")

    if not tasks:
        return print("No tasks logged yet.")

    total_success, total_failed, total_pending = 0, 0, 0

    for task in tasks:
        task_id = task["task_id"]
        print(f"\n--- task_id={task_id} ---")
        result = check_task(task_id)
        items = result.get("result", {}).get("items", [])

        for item in items:
            status = item.get("status")
            offer_id = item.get("offer_id")
            errors = item.get("errors", [])

            if status in ("imported", "moderating"):
                total_success += 1
                print(f"  OK   {offer_id}: {status}")
            elif errors:
                total_failed += 1
                error_msgs = "; ".join(e.get("message", str(e)) for e in errors)
                print(f"  FAIL {offer_id}: {error_msgs}")
            else:
                total_pending += 1
                print(f"  ...  {offer_id}: {status}")

    print(f"\nTotals: {total_success} succeeded/moderating, {total_failed} failed, {total_pending} pending.")
    if total_failed:
        print("Re-run this script after fixing mappings for failed items, or check upload_skipped.jsonl / mapping_log.jsonl for the mapping decisions made.")


if __name__ == "__main__":
    main()
