"""
Uploads each (article, color)'s generated extra photos (generate_extra_photos.py,
GitHub issue #8: "Ensure 8+ photos per product") to Cloudflare R2, then pushes
the combined photo set (real photos + generated zoom crops, in that order --
real photos stay first/primary) to every live offer_id matching that
article+color, replacing the `images` field. Ozon's /v3/product/import
REPLACES the images list wholesale, it does not append -- confirmed live,
2026-09-03, via /v4/product/info/attributes showing a plain list with no
separate "extra images" field -- so every push must submit the full combined
list, not just the new crops.

Matching is done per-offer_id by reconstructing the expected offer_id from
(article_code, color, eu_size) via upload_to_ozon.build_sku() and checking it
against the live catalog -- same pattern as push_videos_to_ozon.py, for the
same reason (an article with multiple colors would otherwise risk a
string-parsed color match picking the wrong color's photos for another
color's offer_id). Only MS-prefix offer_ids are in scope for this reason.

Submitted ONE ITEM AT A TIME and verified via a direct /v4/product/info/attributes
re-read after each push, never trusting /v1/product/import/info's batch
status -- confirmed live, 2026-08-28 (see push_videos_to_ozon.py docstring):
100-item batches reported "imported, zero errors" for ~94% of items that, on
direct re-check, never actually got the pushed change.

Deletes the local generated_photos/ folder and its R2 copies once every
offer_id for that (article, color) has been confirmed successful, matching
the "don't keep generated media sitting around after a successful push"
pattern already established for videos.

Not part of daily_run.py -- a one-time catalog push for the already-generated
crops. New products going forward would need their own crops generated +
pushed the same way (a follow-up, not handled here).
"""
import json
import os
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import boto3
import pandas as pd
from dotenv import load_dotenv

from ozon_client import call
from ozon_mapping import map_size_to_eu
from r2_storage import R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, upload_file
from upload_to_ozon import build_sku

load_dotenv()

PHOTOS_DIR = "generated_photos"
PRODUCTS_CSV = "products.csv"
PUSH_LOG = "extra_photos_push_log.jsonl"


def find_live_ms_offer_ids():
    """Every live (non-archived) MS-prefix offer_id -- the only prefix whose
    offer_id can be reliably reconstructed from (article, color, eu_size)."""
    matches = []
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        for item in items:
            oid = item.get("offer_id", "")
            if not item.get("archived") and oid.startswith("MS-"):
                matches.append(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return matches


def fetch_attributes(offer_ids):
    offer_ids = list(offer_ids)
    records = {}
    for i in range(0, len(offer_ids), 1000):
        batch = offer_ids[i:i + 1000]
        cursor = ""
        while True:
            params = {"filter": {"offer_id": batch}, "limit": 1000}
            if cursor:
                params["last_id"] = cursor
            result = call("/v4/product/info/attributes", params)
            for item in result.get("result", []):
                records[item["offer_id"]] = item
            cursor = result.get("last_id")
            if not cursor or not result.get("result"):
                break
    return records


def fetch_prices(offer_ids):
    offer_ids = list(offer_ids)
    prices = {}
    for i in range(0, len(offer_ids), 1000):
        batch = offer_ids[i:i + 1000]
        cursor = ""
        while True:
            params = {"filter": {"offer_id": batch}, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = call("/v5/product/info/prices", params)
            for item in result.get("items", []):
                price_info = item.get("price", {})
                prices[item["offer_id"]] = {
                    "price": str(price_info.get("price", "")),
                    "currency_code": price_info.get("currency_code", "USD"),
                    "vat": str(price_info.get("vat", "0")),
                }
            cursor = result.get("cursor")
            if not cursor or not result.get("items"):
                break
    return prices


def build_photo_item(record, combined_images, price_info):
    return {
        "offer_id": record["offer_id"],
        "name": record["name"],
        "description_category_id": record["description_category_id"],
        "type_id": record["type_id"],
        "attributes": record["attributes"],
        "price": price_info["price"],
        "currency_code": price_info["currency_code"],
        "vat": price_info["vat"],
        "images": combined_images,
        "primary_image": record.get("primary_image") or (combined_images[0] if combined_images else None),
        "weight": record.get("weight"),
        "weight_unit": record.get("weight_unit"),
        "depth": record.get("depth"),
        "width": record.get("width"),
        "height": record.get("height"),
        "dimension_unit": record.get("dimension_unit"),
    }


def delete_r2_objects(keys):
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    for key in keys:
        try:
            client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        except Exception as e:
            print(f"    ! could not delete R2 object {key}: {e}")


def delete_local_photo_dir(folder_key):
    import shutil
    product_dir = os.path.join(PHOTOS_DIR, folder_key)
    if os.path.isdir(product_dir):
        shutil.rmtree(product_dir, ignore_errors=True)


def main():
    if not os.path.isdir(PHOTOS_DIR):
        return print(f"{PHOTOS_DIR}/ not found. Run generate_extra_photos.py first.")

    try:
        products_df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    folder_keys = [d for d in os.listdir(PHOTOS_DIR) if os.path.isdir(os.path.join(PHOTOS_DIR, d))]

    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
    if limit:
        folder_keys = folder_keys[:limit]
        print(f"--limit={limit}: testing on the first {len(folder_keys)} folder(s) only.\n")

    print(f"{len(folder_keys)} (article, color) folder(s) have generated extra photos.\n")

    print("Finding live MS-prefix offer_ids...")
    live_ms_offer_ids = set(find_live_ms_offer_ids())
    print(f"{len(live_ms_offer_ids)} live MS-prefix offer_id(s) found.\n")

    # For every row in products.csv, reconstruct its expected offer_id and
    # check if it's live -- same (article, color, size) -> offer_id
    # reconstruction as push_videos_to_ozon.py, avoiding any string-parsing
    # of color back out of an offer_id.
    offer_to_folder_key = {}
    offer_to_real_photos = {}
    for _, row in products_df.iterrows():
        article_code = row.get("ms_article_code")
        color = row.get("color")
        if pd.isna(article_code) or not article_code:
            continue
        folder_key = f"{article_code}_{(color or '').replace(' ', '').upper()}"
        if folder_key not in folder_keys:
            continue
        eu_size, _ = map_size_to_eu(row.get("size_label"))
        if not eu_size:
            continue
        offer_id = build_sku(article_code, color, eu_size)
        if offer_id in live_ms_offer_ids:
            offer_to_folder_key[offer_id] = folder_key
            offer_to_real_photos[offer_id] = [
                u.strip() for u in str(row.get("image_urls") or "").split("|") if u.strip()
            ]

    print(f"{len(offer_to_folder_key)} live offer_id(s) matched to a generated (article, color) photo set.\n")

    if not offer_to_folder_key:
        return print("Nothing to push.")

    print("Fetching live attributes and prices...")
    records = fetch_attributes(offer_to_folder_key.keys())
    prices = fetch_prices(offer_to_folder_key.keys())

    # Upload each (article, color)'s generated crops to R2 ONCE, reuse the
    # same URLs for every offer_id (i.e. every size) sharing that article+color.
    folder_to_crop_urls = {}
    for folder_key in set(offer_to_folder_key.values()):
        product_dir = os.path.join(PHOTOS_DIR, folder_key)
        crop_files = sorted(f for f in os.listdir(product_dir) if f.lower().endswith(".jpg"))
        urls = []
        for filename in crop_files:
            local_path = os.path.join(product_dir, filename)
            try:
                urls.append(upload_file(local_path, f"extra_photos/{folder_key}/{filename}"))
            except Exception as e:
                print(f"  ! R2 upload failed for {folder_key}/{filename}: {e}")
        if urls:
            folder_to_crop_urls[folder_key] = urls

    print(f"{len(folder_to_crop_urls)} (article, color) photo set(s) uploaded to R2.\n")

    to_submit = []
    for oid, folder_key in offer_to_folder_key.items():
        record = records.get(oid)
        price_info = prices.get(oid)
        crop_urls = folder_to_crop_urls.get(folder_key)
        if not record or not price_info or not price_info.get("price") or not crop_urls:
            continue
        real_photos = offer_to_real_photos.get(oid, [])
        combined_images = real_photos + crop_urls
        item = build_photo_item(record, combined_images, price_info)
        to_submit.append((oid, folder_key, item, len(combined_images)))

    print(f"{len(to_submit)} item(s) ready to submit.\n")

    # Submitted ONE AT A TIME, not batched -- see module docstring.
    total_ok, total_failed = 0, 0
    succeeded_folders = set()
    for idx, (oid, folder_key, item, expected_count) in enumerate(to_submit, 1):
        if idx % 50 == 0 or idx == len(to_submit):
            print(f"  ... {idx}/{len(to_submit)}")
        try:
            result = call("/v3/product/import", {"items": [item]})
            task_id = result.get("result", {}).get("task_id")
            time.sleep(3)
            info = call("/v1/product/import/info", {"task_id": task_id})
            info_items = info.get("result", {}).get("items", [])
            status_entry = info_items[0] if info_items else {}

            # Trust ONLY a direct re-read of the actual images field, not
            # the import/info status -- see module docstring. A single
            # re-check right after the 3s wait was found live, 2026-09-03,
            # to sometimes read STALE data (Ozon's write hadn't propagated
            # yet) -- confirmed by re-checking the same offer_id moments
            # later and finding the correct, fully-updated image count. So
            # retry the re-check a few times with a short backoff before
            # concluding the push actually failed.
            actual_count = 0
            for attempt in range(4):
                if attempt:
                    time.sleep(4)
                check = call("/v4/product/info/attributes", {"filter": {"offer_id": [oid]}, "limit": 1})
                check_items = check.get("result", [])
                actual_count = len(check_items[0].get("images", [])) if check_items else 0
                if actual_count >= expected_count:
                    break
            verified = actual_count >= expected_count

            with open(PUSH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "offer_id": oid, "folder_key": folder_key,
                    "status": status_entry.get("status"), "errors": status_entry.get("errors"),
                    "expected_count": expected_count, "actual_count": actual_count,
                    "verified_live": verified,
                }, ensure_ascii=False) + "\n")

            if verified:
                total_ok += 1
                succeeded_folders.add(folder_key)
            else:
                total_failed += 1
                print(f"  FAIL {oid}: status={status_entry.get('status')} errors={status_entry.get('errors')} "
                      f"(expected {expected_count} images, found {actual_count} on re-check)")
        except Exception as e:
            print(f"  ! {oid} failed: {e}")
            total_failed += 1

    print(f"\n{total_ok} offer_id(s) updated successfully, {total_failed} failed.")

    # Clean up local + R2 copies for (article, color) folders where EVERY
    # offer_id succeeded -- same pattern as push_videos_to_ozon.py.
    folders_with_any_failure = {fk for oid, fk, _, _ in to_submit if fk not in succeeded_folders}
    fully_succeeded = succeeded_folders - folders_with_any_failure
    print(f"\nCleaning up {len(fully_succeeded)} fully-succeeded (article, color) folder(s) (local + R2)...")
    for folder_key in fully_succeeded:
        product_dir = os.path.join(PHOTOS_DIR, folder_key)
        try:
            crop_files = [f for f in os.listdir(product_dir) if f.lower().endswith(".jpg")]
            delete_r2_objects([f"extra_photos/{folder_key}/{f}" for f in crop_files])
        except FileNotFoundError:
            pass
        delete_local_photo_dir(folder_key)

    print("Done.")


if __name__ == "__main__":
    main()
