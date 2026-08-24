"""
Daily stock refresh for M&S-sourced products already live on Ozon (MS-* offer_ids).

Unlike update_stocks.py (which only pushes stock for rows already sitting in
products.csv from a crawl), this re-fetches each live product's actual M&S page
today and pushes a fresh stock count — covering products this pipeline never
directly crawled (older manual uploads), as long as their M&S source URL is
known.

Source of URLs, in priority order:
  1. legacy_product_urls.csv — a direct offer_id -> M&S URL mapping the business
     supplied for products uploaded before this pipeline tracked source URLs.
  2. products.csv — this pipeline's own crawl history (ms_article_code/parent_sku
     matched against live offer_ids, same logic as category_priority.py).

Live MS-* offer_ids with no URL in either source are left untouched — not
modified, not reported as broken. That's most of the ~10,900 legacy catalog
until legacy_product_urls.csv is extended to cover them.
"""
import sys
import time

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import re

from crawler import extract_product
from ozon_client import call
from category_priority import fetch_live_ms_identifiers

LEGACY_URL_MAP_FILE = "legacy_product_urls.csv"
PRODUCTS_CSV = "products.csv"
WAREHOUSE_ID = 1020000320456000  # "Ozpark Bee Concept" — the account's only warehouse
FALLBACK_STOCK = 20
BATCH_SIZE = 100  # Ozon's max per /v2/products/stocks call
REFETCH_LOG = "stock_refresh_skipped.jsonl"


def load_legacy_url_map(path=LEGACY_URL_MAP_FILE):
    """offer_id -> M&S URL, from the business-supplied mapping. Returns {} if the
    file doesn't exist yet (it's optional)."""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    return dict(zip(df["ID"], df["URL"]))


def load_pipeline_url_map(live_offer_ids, path=PRODUCTS_CSV):
    """offer_id -> M&S URL, derived by matching products.csv's ms_article_code/
    parent_sku against live offer_ids (substring match, same as category_priority's
    product_already_live), for products this pipeline itself crawled."""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return {}

    url_map = {}
    for _, row in df.drop_duplicates("ms_article_code").iterrows():
        article_code = row.get("ms_article_code")
        parent_sku = row.get("parent_sku")
        url = row.get("url")
        if pd.isna(url):
            continue
        for oid in live_offer_ids:
            if oid in url_map:
                continue
            if (article_code and str(article_code) in oid) or (parent_sku and str(parent_sku) in oid):
                url_map[oid] = url
    return url_map


def extract_size_token(offer_id):
    """MS-T61004100-KAHVE-34 -> '34', MS-T81006849L-PINKMIX-40 -> '40'.
    The size/RU-size token is always the last hyphen-separated segment."""
    return offer_id.rsplit("-", 1)[-1] if "-" in offer_id else None


def build_stock_updates_for_url(url, offer_ids_for_url):
    """Re-fetches one M&S product page and returns [{offer_id, stock}, ...] for
    every offer_id in offer_ids_for_url whose size matches a variant on the page.
    An offer_id with no matching size variant is skipped and logged, not guessed."""
    try:
        variant_rows = extract_product(url)
    except Exception as e:
        return [], f"fetch error: {e}"

    if not variant_rows:
        return [], "no product data found on page"

    # Match each fresh page variant to an offer_id by its size token. This can't
    # assume the offer_id's trailing token was built via ozon_mapping's UK->RU
    # conversion table — legacy/manually-uploaded offer_ids were confirmed (on
    # real data) to instead use the EU size number M&S displays directly on the
    # page (e.g. label "46 (UK 18)" -> offer_id ends "-46", not "-52" as the
    # UK->RU table would give for UK 18). So we match on BOTH candidates: the
    # label's own leading EU number, and the letter size when present. Whichever
    # one is present on the label is tried; a numeric offer_id token only matches
    # a numeric label EU number, a letter token only matches a letter label.
    fresh_by_size_token = {}
    for v in variant_rows:
        label = (v.get("size_label") or "").strip()
        stock = v.get("stock_count")
        stock = stock if stock is not None else 0

        eu_match = re.match(r"^(\d+)\s*\(", label)
        letter_match = re.search(r"\b(XXL|XS|S|M|L|XL)\b", label)

        if eu_match:
            fresh_by_size_token[eu_match.group(1)] = stock
        if letter_match:
            fresh_by_size_token[letter_match.group(1)] = stock

    updates = []
    unmatched = []
    for offer_id in offer_ids_for_url:
        size_token = extract_size_token(offer_id)
        if size_token and size_token.upper() in fresh_by_size_token:
            updates.append({"offer_id": offer_id, "stock": fresh_by_size_token[size_token.upper()]})
        elif size_token and size_token in fresh_by_size_token:
            updates.append({"offer_id": offer_id, "stock": fresh_by_size_token[size_token]})
        else:
            unmatched.append(offer_id)

    warning = f"{len(unmatched)} offer_id(s) had no matching size on the fresh page" if unmatched else None
    return updates, warning


def push_stock_updates(updates):
    total_ok, total_failed = 0, 0
    retry_queue = []
    for i in range(0, len(updates), BATCH_SIZE):
        batch = updates[i:i + BATCH_SIZE]
        payload_stocks = [{"offer_id": u["offer_id"], "stock": u["stock"], "warehouse_id": WAREHOUSE_ID} for u in batch]
        print(f"Updating batch {i // BATCH_SIZE + 1} ({len(batch)} items)...")
        result = call("/v2/products/stocks", {"stocks": payload_stocks})
        for r in result.get("result", []):
            if r.get("updated"):
                total_ok += 1
            elif any(e.get("code") == "TOO_MANY_REQUESTS" for e in r.get("errors", [])):
                retry_queue.append(next(u for u in batch if u["offer_id"] == r["offer_id"]))
            else:
                total_failed += 1
                errors = "; ".join(e.get("message", str(e)) for e in r.get("errors", []))
                print(f"  FAIL {r.get('offer_id')}: {errors}")

    if retry_queue:
        print(f"\n{len(retry_queue)} item(s) hit the per-offer rate limit — waiting 20s and retrying once...")
        time.sleep(20)
        payload_stocks = [{"offer_id": u["offer_id"], "stock": u["stock"], "warehouse_id": WAREHOUSE_ID} for u in retry_queue]
        result = call("/v2/products/stocks", {"stocks": payload_stocks})
        for r in result.get("result", []):
            if r.get("updated"):
                total_ok += 1
            else:
                total_failed += 1
                errors = "; ".join(e.get("message", str(e)) for e in r.get("errors", []))
                print(f"  FAIL (after retry) {r.get('offer_id')}: {errors}")

    return total_ok, total_failed


def main():
    print("Fetching live MS-* offer_ids from Ozon...")
    live_offer_ids = fetch_live_ms_identifiers()
    print(f"{len(live_offer_ids)} MS-* offer_id(s) live.\n")

    legacy_map = load_legacy_url_map()
    print(f"{len(legacy_map)} offer_id(s) have a known URL via {LEGACY_URL_MAP_FILE}.")

    pipeline_map = load_pipeline_url_map(live_offer_ids)
    print(f"{len(pipeline_map)} offer_id(s) have a known URL via {PRODUCTS_CSV} crawl history.\n")

    # legacy map takes priority since it's an exact per-offer_id mapping;
    # pipeline map fills in anything legacy didn't cover.
    url_for_offer_id = {**pipeline_map, **legacy_map}
    known_offer_ids = set(url_for_offer_id) & live_offer_ids
    unknown_count = len(live_offer_ids) - len(known_offer_ids)

    print(f"{len(known_offer_ids)} live offer_id(s) have a resolvable M&S URL — will be refreshed today.")
    print(f"{unknown_count} live offer_id(s) have no known source URL — left untouched.\n")

    if not known_offer_ids:
        return print("Nothing to refresh.")

    # Group offer_ids by URL so each product page is only fetched once.
    offer_ids_by_url = {}
    for oid in known_offer_ids:
        url = url_for_offer_id[oid]
        offer_ids_by_url.setdefault(url, []).append(oid)

    print(f"{len(offer_ids_by_url)} distinct product page(s) to re-fetch.\n")

    all_updates = []
    fetch_failures = 0
    for i, (url, offer_ids) in enumerate(offer_ids_by_url.items(), 1):
        print(f"[{i}/{len(offer_ids_by_url)}] {url} ({len(offer_ids)} offer_id(s))")
        updates, warning = build_stock_updates_for_url(url, offer_ids)
        if warning:
            print(f"  ! {warning}")
            with open(REFETCH_LOG, "a", encoding="utf-8") as f:
                import json
                f.write(json.dumps({"url": url, "offer_ids": offer_ids, "warning": warning}, ensure_ascii=False) + "\n")
        if not updates and warning and "fetch error" in warning:
            fetch_failures += 1
        all_updates.extend(updates)
        time.sleep(1.2)  # be polite to M&S, same pacing as crawler.py

    print(f"\n{len(all_updates)} stock update(s) ready to push ({fetch_failures} page(s) failed to fetch entirely).\n")

    if not all_updates:
        return print("Nothing to push.")

    total_ok, total_failed = push_stock_updates(all_updates)
    print(f"\nDone. {total_ok} updated, {total_failed} failed, {unknown_count} skipped (no known URL).")


if __name__ == "__main__":
    main()
