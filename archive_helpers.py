"""
Shared batch-archive mechanics used by the one-off cleanup scripts
(archive_unmapped_products.py, archive_by_offer_id_pattern.py). Not part of
daily_run.py — archiving live products is always a deliberate, by-hand
decision, never something the daily automation does on its own.
"""
import json
import time

from ozon_client import call

BATCH_SIZE = 100  # conservative; Ozon's documented product/archive limit is 100 per call


def resolve_product_ids(offer_ids):
    """offer_id -> product_id for currently-unarchived items, via
    /v3/product/list, batched at 1000 (API max) for the filter itself."""
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


def archive_offer_ids(offer_ids, log_path):
    """Resolves offer_ids to product_ids, archives them in batches of 100, and
    logs every batch (started/ok/failed) to log_path (JSONL, gitignored) before
    and after each API call so the exact scope touched is always recoverable
    via /v1/product/archive's counterpart, /v1/product/unarchive.

    Returns (total_ok, total_failed, already_archived_count)."""
    print("Resolving to product_ids and filtering out anything already archived...")
    offer_to_product = resolve_product_ids(offer_ids)
    already_archived = len(set(offer_ids)) - len(offer_to_product)
    print(f"{len(offer_to_product)} to archive now, {already_archived} already archived.\n")

    if not offer_to_product:
        return 0, 0, already_archived

    with open(log_path, "a", encoding="utf-8") as f:
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
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": "archive_batch_failed",
                    "product_ids": batch,
                    "offer_ids": [id_to_offer[pid] for pid in batch],
                    "error": str(e),
                }, ensure_ascii=False) + "\n")
            continue

        if result.get("result") is True:
            total_ok += len(batch)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": "archive_batch_ok",
                    "product_ids": batch,
                    "offer_ids": [id_to_offer[pid] for pid in batch],
                }, ensure_ascii=False) + "\n")
        else:
            total_failed += len(batch)
            print(f"  ! unexpected response: {result}")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "action": "archive_batch_unexpected_response",
                    "product_ids": batch,
                    "offer_ids": [id_to_offer[pid] for pid in batch],
                    "response": result,
                }, ensure_ascii=False) + "\n")

        time.sleep(1)

    return total_ok, total_failed, already_archived


def archive_offer_ids_individually(offer_ids, log_path):
    """Retries a small set of offer_ids one at a time — used to isolate
    FBO-stock-blocked items (Ozon rejects an entire batch if any one member
    has FBO stock) from genuinely archivable ones. Returns (ok, fbo_blocked,
    other_failures) lists of offer_ids."""
    offer_to_product = resolve_product_ids(offer_ids)
    ok, fbo_blocked, other_failures = [], [], []
    for oid, pid in offer_to_product.items():
        try:
            r = call("/v1/product/archive", {"product_id": [pid]})
            if r.get("result") is True:
                ok.append(oid)
            else:
                other_failures.append((oid, r))
        except Exception as e:
            if "fbo stock" in str(e).lower():
                fbo_blocked.append(oid)
            else:
                other_failures.append((oid, str(e)))
        time.sleep(0.3)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"action": "individual_retry_ok", "offer_ids": ok}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"action": "individual_retry_fbo_stock_blocked", "offer_ids": fbo_blocked}, ensure_ascii=False) + "\n")
        if other_failures:
            f.write(json.dumps({"action": "individual_retry_other_failures", "items": other_failures}, ensure_ascii=False) + "\n")

    return ok, fbo_blocked, other_failures
