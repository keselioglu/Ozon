"""
Small append-only per-day log of NEW Ozon offers created ->
daily_created_history.jsonl (gitignored via *.jsonl).

daily_report_history.jsonl only has a 10am "products_added" snapshot (it
misses the 10:15 quota-fill and has no per-category split), so
upload_to_ozon.py appends one line here after every run that created
something:

    {"date": "YYYY-MM-DD", "created_total": N, "by_category": {"Pajama": n, ...},
     "recorded_at": ISO-8601}

created_total / by_category are the running totals for that UTC day (all
runs so far), so the LAST line for a date is the day's figure. Read by
category_inventory.py for its 14-day history. Local file only, no API.
"""
import json
from collections import Counter
from datetime import datetime, timezone

from daily_plan import CATEGORY_LABELS, utc_today

HISTORY_FILE = "daily_created_history.jsonl"
UNMAPPED = "Unmapped"


def load_history(path=HISTORY_FILE):
    """{date: last record for that date}. Missing/corrupt file -> {}."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("date"), str):
                    out[rec["date"]] = rec
    except OSError:
        pass
    return out


def record_run(new_items, created_total, today=None, path=HISTORY_FILE):
    """Appends today's running totals after an upload run. new_items are the
    /v3/product/import items created THIS run (their type_id gives the
    category); earlier runs' per-category counts come from the previous line
    for today. Never raises -- a failed history write must not fail the
    upload."""
    try:
        today = today or utc_today()
        prior = load_history(path).get(today) or {}
        by_cat = Counter(prior.get("by_category") or {})
        for it in new_items or []:
            by_cat[CATEGORY_LABELS.get(it.get("type_id"), UNMAPPED)] += 1
        rec = {
            "date": today,
            "created_total": int(created_total),
            "by_category": dict(sorted(by_cat.items())),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:
        print(f"Could not append to {path}: {e}")
        return None
