"""
One-off cleanup: archives every live MS-* product on Ozon that has no known
M&S source URL (neither in legacy_product_urls.csv nor derivable from
products.csv), since refresh_live_stock.py can never verify or refresh their
stock without a source page. Confirmed via /v3/product/list + /v1/product/archive
on a live test item before running at scale (see conversation history).

This is NOT part of the daily automation (daily_run.py) — it's a deliberate,
one-time decision to stop selling products this pipeline can't maintain, run
by hand. Re-running it later is safe (already-archived products are simply
skipped) but it won't un-archive anything.

Every archived product_id/offer_id is logged to archived_unmapped_log.jsonl
before the API call, so the exact set touched is always recoverable via
/v1/product/unarchive if this needs to be reversed.
"""
import json
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from category_priority import fetch_live_ms_identifiers
from refresh_live_stock import load_legacy_url_map, load_pipeline_url_map

ARCHIVE_LOG = "archived_unmapped_log.jsonl"
BATCH_SIZE = 100  # conservative; Ozon's documented product/archive limit is 100 per call


def find_unmapped_offer_ids():
    """Returns the set of live MS-* offer_ids with no known M&S source URL."""
    live_offer_ids = fetch_live_ms_identifiers()
    legacy_map = load_legacy_url_map()
    pipeline_map = load_pipeline_url_map(live_offer_ids)
    known = (set(legacy_map) | set(pipeline_map)) & live_offer_ids
    return live_offer_ids - known


def resolve_product_ids(offer_ids):
    """offer_id -> product_id, via /v3/product/list, batched at 1000 (API max)."""
    offer_ids = list(offer_ids)
    mapping = {}
    for i in range(0, len(offer_ids), 1000):
        batch = offer_ids[i:i + 1000]
        cursor = ""
        while True:
            params = {"filter": {"offer_id": batch}, "limit": 1000}
            if cursor:
                params["last_id"] = cursor
            result = call("/v3/product/list", params)
            page = result.get("result", {})
            for item in page.get("items", []):
                if not item.get("archived"):  # skip already-archived, nothing to do
                    mapping[item["offer_id"]] = item["product_id"]
            cursor = page.get("last_id")
            if not cursor or not page.get("items"):
                break
    return mapping


def main():
    print("Finding live MS-* products with no known M&S source URL...")
    unmapped_offer_ids = find_unmapped_offer_ids()
    print(f"{len(unmapped_offer_ids)} unmapped offer_id(s) found.\n")

    if not unmapped_offer_ids:
        return print("Nothing to archive.")

    print("Resolving to product_ids and filtering out anything already archived...")
    offer_to_product = resolve_product_ids(unmapped_offer_ids)
    already_archived = len(unmapped_offer_ids) - len(offer_to_product)
    print(f"{len(offer_to_product)} to archive now, {already_archived} already archived.\n")

    if not offer_to_product:
        return print("Nothing left to archive.")

    # Log the full set before touching the API, so the exact scope is on record
    # even if the run is interrupted partway through.
    with open(ARCHIVE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "action": "archive_batch_started",
            "count": len(offer_to_product),
            "offer_ids": sorted(offer_to_product.keys()),
        }, ensure_ascii=False) + "\n")

    product_ids = list(offer_to_product.values())
    id_to_offer = {v: k for k, v in offer_to_product.items()}

    total_ok, total_failed = 0, 0
    for i in range(0, len(product_ids), BATCH_SIZE):
        batch = product_ids[i:i + BATCH_SIZE]
        print(f"Archiving batch {i // BATCH_SIZE + 1} ({len(batch)} product(s))...")
        try:
            result = call("/v1/product/archive", {"product_id": batch})
        except Exception as e:
            print(f"  ! batch failed: {e}")
            total_failed += len(batch)
            with open(ARCHIVE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": "archive_batch_failed",
                    "product_ids": batch,
                    "offer_ids": [id_to_offer[pid] for pid in batch],
                    "error": str(e),
                }, ensure_ascii=False) + "\n")
            continue

        if result.get("result") is True:
            total_ok += len(batch)
            with open(ARCHIVE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": "archive_batch_ok",
                    "product_ids": batch,
                    "offer_ids": [id_to_offer[pid] for pid in batch],
                }, ensure_ascii=False) + "\n")
        else:
            total_failed += len(batch)
            print(f"  ! unexpected response: {result}")
            with open(ARCHIVE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": "archive_batch_unexpected_response",
                    "product_ids": batch,
                    "offer_ids": [id_to_offer[pid] for pid in batch],
                    "response": result,
                }, ensure_ascii=False) + "\n")

        time.sleep(1)

    print(f"\nDone. {total_ok} archived, {total_failed} failed.")
    print(f"Full record in {ARCHIVE_LOG} — every offer_id/product_id touched, for un-archiving if needed.")


if __name__ == "__main__":
    main()
