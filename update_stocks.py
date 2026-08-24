"""
Pushes real stock counts (from products.csv) to Ozon for every already-uploaded
product (offer_id must already exist on Ozon — this does not create new products).
Falls back to FALLBACK_STOCK when the crawled stock_count is missing.
"""
import sys
import time

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from upload_to_ozon import build_sku, find_existing_offer_ids

PRODUCTS_CSV = "products.csv"
WAREHOUSE_ID = 1020000320456000  # "Ozpark Bee Concept" — the account's only warehouse
FALLBACK_STOCK = 20
BATCH_SIZE = 100  # Ozon's max per /v2/products/stocks call


def build_stock_updates(df):
    """Returns list of {offer_id, stock} for every row with a resolvable SKU,
    using the real crawled stock_count or FALLBACK_STOCK when missing."""
    updates = []
    for _, row in df.iterrows():
        article_code = row.get("ms_article_code")
        if pd.isna(article_code) or not article_code:
            continue

        # Re-derive the RU size the same way build_ozon_item does, so offer_id matches
        # exactly what was actually uploaded.
        from ozon_mapping import map_size_to_ozon
        _, ru_size, _ = map_size_to_ozon(row.get("size_label"))
        if not ru_size:
            continue

        offer_id = build_sku(article_code, row.get("color"), ru_size)

        stock_count = row.get("stock_count")
        stock = int(stock_count) if pd.notna(stock_count) else FALLBACK_STOCK

        updates.append({"offer_id": offer_id, "stock": stock})
    return updates


def main():
    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    all_updates = build_stock_updates(df)
    print(f"{len(all_updates)} candidate stock update(s) from {PRODUCTS_CSV}.")

    # Only push stock for offer_ids that actually exist on Ozon — this script
    # updates, it never creates.
    offer_ids = [u["offer_id"] for u in all_updates]
    existing = find_existing_offer_ids(offer_ids)
    updates = [u for u in all_updates if u["offer_id"] in existing]
    skipped_not_uploaded = len(all_updates) - len(updates)

    print(f"{len(updates)} of those already exist on Ozon (will be updated).")
    print(f"{skipped_not_uploaded} skipped — not yet uploaded as a product.\n")

    if not updates:
        return print("Nothing to update.")

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

    print(f"\nDone. {total_ok} updated, {total_failed} failed.")


if __name__ == "__main__":
    main()
