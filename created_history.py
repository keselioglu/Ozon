"""
Small append-only per-day log of NEW Ozon offers created ->
daily_created_history.jsonl (gitignored via *.jsonl).

daily_report_history.jsonl only has a 10am "products_added" snapshot (it
misses the 10:15 quota-fill and has no per-category split), so this file
keeps its own per-day record:

    {"date": "YYYY-MM-DD", "created_total": N, "by_category": {"Pajama": n, ...},
     "recorded_at": ISO-8601}

The figures are copied from new_items_submitted.json (upload_to_ozon's
running list of the day's created offer_ids), so upload_to_ozon itself is
untouched. `category_inventory.py --record-history` calls record_snapshot();
daily_run.py runs that at the START of the 4am run (the file still holds
yesterday's final list, 10:15 quota-fill included) and again at the end.
The LAST line for a date is the day's figure. Local files only, no API.
"""
import json
from collections import Counter
from datetime import datetime, timezone

from ozon_mapping import extract_article_code_from_offer_id

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


def summarize_new_items(new_items_log, article_labels):
    """(date, created_total, by_category) from a new_items_submitted.json
    dict, or None if it has no date. Offers whose article code isn't in
    article_labels count as Unmapped."""
    if not isinstance(new_items_log, dict) or not isinstance(new_items_log.get("date"), str):
        return None
    ids = [o for o in new_items_log.get("new_offer_ids") or [] if isinstance(o, str)]
    by_cat = Counter()
    for oid in ids:
        by_cat[(article_labels or {}).get(extract_article_code_from_offer_id(oid)) or UNMAPPED] += 1
    return new_items_log["date"], len(ids), dict(sorted(by_cat.items()))


def record_snapshot(new_items_log, article_labels, path=HISTORY_FILE):
    """Appends the day's totals from new_items_submitted.json, unless the
    last line for that date already says the same (so repeated calls don't
    grow the file) or the count went DOWN (never overwrite a bigger figure
    with a smaller one). Returns the record written, else None. Never
    raises -- a failed history write must not fail a pipeline step."""
    try:
        summary = summarize_new_items(new_items_log, article_labels)
        if summary is None:
            return None
        day, total, by_cat = summary
        prior = load_history(path).get(day) or {}
        prior_total = prior.get("created_total")
        if isinstance(prior_total, int) and (
                total < prior_total or (total == prior_total and prior.get("by_category") == by_cat)):
            return None
        rec = {
            "date": day,
            "created_total": total,
            "by_category": by_cat,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:
        print(f"Could not append to {path}: {e}")
        return None
