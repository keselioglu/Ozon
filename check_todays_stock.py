"""
6am step (business instruction, 2026-08-31): check whether TODAY's newly
created products have real stock live on Ozon, and push it if not.

Newly uploaded products can take a while to clear Ozon's moderation queue
before stock can even be pushed (confirmed live, repeatedly, in issues #9/
#12) -- by 6am, roughly 2 hours after the (now 4am) upload step and the 5am
quota top-up, many of that day's new offer_ids should have cleared, so this
is a second, earlier-in-the-day chance to get their real stock live instead
of waiting for the next full daily_run.py's stock sync.

Scoped to ONLY today's new-product offer_ids (from new_items_submitted.json,
written by upload_to_ozon.py) -- not the whole catalog, which is what
update_stocks.py's regular run already covers. Reuses update_stocks.py's
own stock-building and push/retry logic (push_stock_updates_for) rather
than duplicating it.
"""
import json
import sys
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from update_stocks import PRODUCTS_CSV, build_stock_updates, push_stock_updates_for
from upload_to_ozon import find_existing_offer_ids

NEW_ITEMS_LOG = "new_items_submitted.json"


def main():
    try:
        with open(NEW_ITEMS_LOG, encoding="utf-8") as f:
            new_items = json.load(f)
    except FileNotFoundError:
        return print(f"{NEW_ITEMS_LOG} not found -- nothing submitted as new today (or upload step hasn't run yet).")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if new_items.get("date") != today:
        return print(f"{NEW_ITEMS_LOG} is from {new_items.get('date')}, not today ({today}) -- "
                      "today's upload step may not have run yet (or failed). Not re-checking stale data.")

    todays_offer_ids = set(new_items.get("new_offer_ids", []))
    print(f"{len(todays_offer_ids)} offer_id(s) submitted as new today ({new_items.get('date')}).\n")

    if not todays_offer_ids:
        return print("Nothing to check.")

    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    all_updates = build_stock_updates(df)
    todays_updates = [u for u in all_updates if u["offer_id"] in todays_offer_ids]
    print(f"{len(todays_updates)} of today's offer_id(s) have a resolvable stock value from products.csv.\n")

    if not todays_updates:
        return print("Nothing to update -- today's offer_ids may not be in products.csv "
                      "(e.g. legacy-prefix uploads not covered by this pipeline's own crawl).")

    print("Confirming which of today's offer_ids are actually live on Ozon yet...")
    existing = find_existing_offer_ids([u["offer_id"] for u in todays_updates])
    existing_updates = [u for u in todays_updates if u["offer_id"] in existing]
    not_yet_live = len(todays_updates) - len(existing_updates)
    print(f"{len(existing_updates)} live, {not_yet_live} not live yet (still being created).\n")

    push_stock_updates_for(existing_updates)


if __name__ == "__main__":
    main()
