"""
Daily M&S price tracking for products already live on Ozon (business
instruction, 2026-09-02: "do we have a data of updated prices on marks&spencer
daily? ... yes build it and update it daily").

Nothing in this pipeline previously re-checked M&S's price after a product
was first crawled -- update_stocks.py/refresh_live_stock.py re-check STOCK
daily but reuse whatever price was captured at crawl time. This script closes
that gap for price specifically, without touching stock or pushing anything
to Ozon (this pipeline has never pushed prices to Ozon; that's a separate,
not-yet-requested feature).

Reuses refresh_live_stock.py's own offer_id -> M&S URL resolution (legacy
mapping + products.csv crawl history, confirmed-live filtering) rather than
duplicating it, so "which live products have a known source URL" stays a
single source of truth. Each distinct URL is fetched once (already required
for accuracy at this catalog's scale -- see refresh_live_stock.py's polite
1.2s pacing) via crawler.extract_product(), which returns price/currency per
product already (previously discarded by refresh_live_stock.py since it only
needed stock).

Writes two files:
  - price_history.jsonl (append-only, one line per offer_id per run: date,
    offer_id, url, price, currency) -- the full daily record, so a price
    trend can be reconstructed for any product later.
  - price_changes_today.json (overwritten each run) -- only offer_ids whose
    price differs from their last known value in price_history.jsonl, for a
    quick daily read without scanning the full history.

Wired into daily_run.py as its own step (after refresh_live_stock.py, so
both share the "known offer_id -> URL" resolution logic and run
back-to-back).
"""
import json
import sys
import time
from datetime import date

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from crawler import extract_product
from category_priority import fetch_live_ms_identifiers
from refresh_live_stock import (
    fetch_live_offer_ids_matching,
    load_legacy_url_map,
    load_pipeline_url_map,
    verify_url_matches_offer_id,
)

PRICE_HISTORY_FILE = "price_history.jsonl"
PRICE_CHANGES_FILE = "price_changes_today.json"


def load_last_known_prices(path=PRICE_HISTORY_FILE):
    """offer_id -> (price, currency, date) from the most recent entry for
    that offer_id in the history file. Returns {} if the file doesn't exist
    yet (first run)."""
    last = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                last[entry["offer_id"]] = (entry.get("price"), entry.get("currency"), entry.get("date"))
    except FileNotFoundError:
        pass
    return last


def fetch_prices_for_url(url, offer_ids_for_url):
    """Re-fetches one M&S product page and returns {offer_id: (price,
    currency)} for every offer_id in offer_ids_for_url whose identity
    verifies against the page (same verify_url_matches_offer_id check
    refresh_live_stock.py uses) -- an offer_id that fails verification is
    omitted rather than assigned a price from the wrong product."""
    try:
        variant_rows = extract_product(url)
    except Exception as e:
        print(f"  ! fetch error: {e}")
        return {}

    if not variant_rows:
        print("  ! no product data found on page")
        return {}

    page_article_code = variant_rows[0].get("ms_article_code")
    page_parent_sku = variant_rows[0].get("parent_sku")
    price = variant_rows[0].get("price")
    currency = variant_rows[0].get("currency")

    if price is None:
        print("  ! no price found on page")
        return {}

    result = {}
    for offer_id in offer_ids_for_url:
        if verify_url_matches_offer_id(offer_id, page_article_code, page_parent_sku):
            result[offer_id] = (price, currency)
    return result


def main():
    today = date.today().isoformat()
    print("Fetching live MS-* offer_ids from Ozon (for products.csv-derived matching)...")
    live_ms_offer_ids = fetch_live_ms_identifiers()

    legacy_map = load_legacy_url_map()
    pipeline_map = load_pipeline_url_map(live_ms_offer_ids)
    url_for_offer_id = {**pipeline_map, **legacy_map}

    print("Confirming which of those offer_ids are actually live right now...")
    known_offer_ids, total_live = fetch_live_offer_ids_matching(url_for_offer_id.keys())
    print(f"{total_live} product(s) live on the account in total.")
    print(f"{len(known_offer_ids)} of them have a resolvable M&S URL — price will be checked today.\n")

    if not known_offer_ids:
        return print("Nothing to check.")

    offer_ids_by_url = {}
    for oid in known_offer_ids:
        url = url_for_offer_id[oid]
        offer_ids_by_url.setdefault(url, []).append(oid)

    print(f"{len(offer_ids_by_url)} distinct product page(s) to re-fetch for price.\n")

    last_known = load_last_known_prices()
    changes = []
    history_lines = []
    fetch_failures = 0

    for i, (url, offer_ids) in enumerate(offer_ids_by_url.items(), 1):
        print(f"[{i}/{len(offer_ids_by_url)}] {url} ({len(offer_ids)} offer_id(s))")
        prices = fetch_prices_for_url(url, offer_ids)
        if not prices:
            fetch_failures += 1
        for offer_id, (price, currency) in prices.items():
            history_lines.append(json.dumps({
                "date": today, "offer_id": offer_id, "url": url,
                "price": price, "currency": currency,
            }, ensure_ascii=False))

            prev_price, prev_currency, prev_date = last_known.get(offer_id, (None, None, None))
            if prev_price is not None and float(prev_price) != float(price):
                changes.append({
                    "offer_id": offer_id, "url": url,
                    "old_price": prev_price, "new_price": price, "currency": currency,
                    "old_date": prev_date,
                })
        time.sleep(1.2)  # be polite to M&S, same pacing as refresh_live_stock.py

    with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
        for line in history_lines:
            f.write(line + "\n")

    with open(PRICE_CHANGES_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": today, "changes": changes}, f, ensure_ascii=False, indent=2)

    print(f"\n{len(history_lines)} price(s) recorded to {PRICE_HISTORY_FILE}.")
    print(f"{len(changes)} price change(s) vs. last known value — see {PRICE_CHANGES_FILE}.")
    print(f"\nDone. {len(history_lines)} checked, {len(changes)} changed, "
          f"{fetch_failures} page(s) failed to fetch, "
          f"{len(known_offer_ids) - sum(len(v) for v in offer_ids_by_url.values())} skipped (no known URL).")


if __name__ == "__main__":
    main()
