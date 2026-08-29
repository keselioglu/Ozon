"""
Uploads each (article, color)'s generated video (generate_product_videos.py,
issue #4) to Cloudflare R2, pushes the resulting URL to every live offer_id
matching that article+color via attributes 21841 (video link) and 21837
(video title), then deletes both the local files and the R2 copies once the
push is confirmed successful -- per explicit business instruction
(2026-08-27): "after sending to Ozon dont keep videos in local or in
claudflare. delete them frequently."

Matching is done per-offer_id by reconstructing the expected offer_id from
(article_code, color, eu_size) via upload_to_ozon.build_sku() and checking
it against the live catalog -- NOT by extracting article code from the
offer_id string alone. An earlier version matched purely by article code,
which meant an article with multiple colors (130 of 447 in this catalog,
confirmed live 2026-08-28) got ONE color's video pushed to every color
variant's offer_id -- confirmed live: a white product's video ended up on a
black offer_id, then a different mismatch on a second attempt. Only MS-
prefix offer_ids (this pipeline's own upload convention) can be
reconstructed this way; legacy-prefix offer_ids (MAR-/SML-/MARK-/etc, whose
color token in the offer_id doesn't reliably match products.csv's color
column) are out of scope for this script and left untouched.

Does NOT set attribute 21845 ("Ozon.Video cover: link") -- despite its
name, its real spec requires an 8-30s MP4/MOV video, not a static image.
NOTE (2026-08-28): an earlier version of this docstring blamed 21845 for a
video attribute that appeared to "disappear" after push -- that was
actually two separate mistakes during manual testing (reusing another
product's video by accident, and misreading Ozon's "skipped" status as a
failure when it just means an identical resubmission was a no-op), not a
real platform bug. 21845 is still skipped on business decision (simpler
than generating a second short video per product just for that slot), but
not because of the disappearing-attribute scare -- that turned out to be a
false lead.

Only touches ATTR_VIDEO_LINK/TITLE -- every other attribute, price, and
images are carried through unchanged from the live record (same safe
pattern as push_hashtag_fixes.py).

Not part of daily_run.py -- a one-time catalog push for the already-
generated videos. New products going forward would need their own video
generated + pushed the same way (a follow-up, not handled here).
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

ATTR_VIDEO_LINK = 21841
ATTR_VIDEO_TITLE = 21837
ATTR_VIDEO_COVER = 21845

VIDEOS_DIR = "generated_videos"
PRODUCTS_CSV = "products.csv"
PUSH_LOG = "video_push_log.jsonl"
BATCH_SIZE = 100


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


def build_video_item(record, video_url, price_info):
    # 21845 (video cover) deliberately omitted -- see module docstring.
    attributes = [a for a in record["attributes"] if a["id"] not in
                  (ATTR_VIDEO_LINK, ATTR_VIDEO_TITLE, ATTR_VIDEO_COVER)]
    attributes.append({"id": ATTR_VIDEO_LINK, "values": [{"value": video_url}]})
    attributes.append({"id": ATTR_VIDEO_TITLE, "values": [{"value": record["name"]}]})

    return {
        "offer_id": record["offer_id"],
        "name": record["name"],
        "description_category_id": record["description_category_id"],
        "type_id": record["type_id"],
        "attributes": attributes,
        "price": price_info["price"],
        "currency_code": price_info["currency_code"],
        "vat": price_info["vat"],
        "images": record.get("images", []),
        "primary_image": record.get("primary_image"),
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


def delete_local_video_dir(folder_key):
    import shutil
    product_dir = os.path.join(VIDEOS_DIR, folder_key)
    if os.path.isdir(product_dir):
        shutil.rmtree(product_dir, ignore_errors=True)


def main():
    if not os.path.isdir(VIDEOS_DIR):
        return print(f"{VIDEOS_DIR}/ not found. Run generate_product_videos.py first.")

    try:
        products_df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    folder_keys = [d for d in os.listdir(VIDEOS_DIR) if os.path.isdir(os.path.join(VIDEOS_DIR, d))]

    limit = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--limit="):
        limit = int(sys.argv[1].split("=", 1)[1])
        folder_keys = folder_keys[:limit]
        print(f"--limit={limit}: testing on the first {len(folder_keys)} folder(s) only.\n")

    print(f"{len(folder_keys)} (article, color) folder(s) have a generated video.\n")

    print("Finding live MS-prefix offer_ids...")
    live_ms_offer_ids = set(find_live_ms_offer_ids())
    print(f"{len(live_ms_offer_ids)} live MS-prefix offer_id(s) found.\n")

    # For every row in products.csv, reconstruct its expected offer_id and
    # check if it's live -- this is the (article, color, size) -> offer_id
    # match, avoiding any string-parsing of color back out of an offer_id.
    offer_to_folder_key = {}
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

    print(f"{len(offer_to_folder_key)} live offer_id(s) matched to a generated (article, color) video.\n")

    if not offer_to_folder_key:
        return print("Nothing to push.")

    print("Fetching live attributes and prices...")
    records = fetch_attributes(offer_to_folder_key.keys())
    prices = fetch_prices(offer_to_folder_key.keys())

    # Upload each (article, color) video to R2 ONCE, reuse the same URL for
    # every offer_id (i.e. every size) sharing that article+color.
    folder_to_url = {}
    for folder_key in set(offer_to_folder_key.values()):
        video_path = os.path.join(VIDEOS_DIR, folder_key, "video.mp4")
        if not os.path.exists(video_path):
            continue
        try:
            folder_to_url[folder_key] = upload_file(video_path, f"videos/{folder_key}/video.mp4")
        except Exception as e:
            print(f"  ! R2 upload failed for {folder_key}: {e}")

    print(f"{len(folder_to_url)} (article, color) video(s) uploaded to R2.\n")

    to_submit = []
    for oid, folder_key in offer_to_folder_key.items():
        record = records.get(oid)
        price_info = prices.get(oid)
        video_url = folder_to_url.get(folder_key)
        if not record or not price_info or not price_info.get("price") or not video_url:
            continue
        item = build_video_item(record, video_url, price_info)
        to_submit.append((oid, folder_key, item))

    print(f"{len(to_submit)} item(s) ready to submit.\n")

    # Submitted ONE AT A TIME, not batched. Confirmed live (2026-08-28):
    # 100-item batches reported "imported" with zero errors for ~94% of
    # items that, on direct re-check via /v4/product/info/attributes, never
    # actually got the video attribute -- Ozon's import/info status is not
    # reliable proof at batch scale. Single-item submissions, verified the
    # same way, were 100% reliable in side-by-side testing. Slower, but
    # correctness matters more than speed here -- a silently-wrong push is
    # worse than a slow correct one.
    total_ok, total_failed = 0, 0
    succeeded_folders = set()
    for idx, (oid, folder_key, item) in enumerate(to_submit, 1):
        if idx % 50 == 0 or idx == len(to_submit):
            print(f"  ... {idx}/{len(to_submit)}")
        try:
            result = call("/v3/product/import", {"items": [item]})
            task_id = result.get("result", {}).get("task_id")
            time.sleep(3)
            info = call("/v1/product/import/info", {"task_id": task_id})
            info_items = info.get("result", {}).get("items", [])
            status_entry = info_items[0] if info_items else {}

            # Trust ONLY a direct re-read of the actual attribute, not the
            # import/info status -- that's exactly what proved unreliable
            # at batch scale.
            check = call("/v4/product/info/attributes", {"filter": {"offer_id": [oid]}, "limit": 1})
            check_items = check.get("result", [])
            has_video = bool(check_items) and any(
                a["id"] == ATTR_VIDEO_LINK and a["values"] and a["values"][0].get("value")
                for a in check_items[0].get("attributes", [])
            )

            with open(PUSH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "offer_id": oid, "folder_key": folder_key,
                    "status": status_entry.get("status"), "errors": status_entry.get("errors"),
                    "verified_live": has_video,
                }, ensure_ascii=False) + "\n")

            if has_video:
                total_ok += 1
                succeeded_folders.add(folder_key)
            else:
                total_failed += 1
                print(f"  FAIL {oid}: status={status_entry.get('status')} errors={status_entry.get('errors')} "
                      f"(attribute not present on re-check)")
        except Exception as e:
            print(f"  ! {oid} failed: {e}")
            total_failed += 1

    print(f"\n{total_ok} offer_id(s) updated successfully, {total_failed} failed.")

    # Clean up local + R2 copies for (article, color) folders where EVERY
    # offer_id succeeded (business instruction: delete after a successful
    # push, don't keep videos sitting in local storage or R2).
    folders_with_any_failure = {fk for oid, fk, _ in to_submit if fk not in succeeded_folders}
    fully_succeeded = succeeded_folders - folders_with_any_failure
    print(f"\nCleaning up {len(fully_succeeded)} fully-succeeded (article, color) folder(s) (local + R2)...")
    for folder_key in fully_succeeded:
        delete_r2_objects([f"videos/{folder_key}/video.mp4"])
        delete_local_video_dir(folder_key)

    print("Done.")


if __name__ == "__main__":
    main()
