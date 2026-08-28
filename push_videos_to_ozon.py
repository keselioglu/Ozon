"""
Uploads each product's generated video (generate_product_videos.py, issue
#4) to Cloudflare R2, pushes the resulting URL to every live offer_id for
that article code via attributes 21841 (video link) and 21837 (video
title), then deletes both the local files and the R2 copies once the push
is confirmed successful -- per explicit business instruction (2026-08-27):
"after sending to Ozon dont keep videos in local or in claudflare. delete
them frequently."

Does NOT set attribute 21845. Its name ("Озон.Видеообложка: ссылка" /
"Ozon.Video cover: link") reads like a static cover image, but its actual
field description requires an 8-30 SECOND MP4/MOV VIDEO, not an image --
confirmed the hard way (2026-08-28): pushing cover.jpg's URL there was
accepted with status "imported" and briefly readable back via the API, but
within a day the ENTIRE video attribute complex disappeared -- not just
21845, but 21841 and 21837 with it. All three share attribute_complex_id
100001/100002, and Ozon appears to validate/reject the complex as a unit:
once its async check found 21845's content invalid (a JPG where an MP4 was
required), it dropped the whole group, including the otherwise-valid video
link and title. R2 hosting itself was never the problem -- the video file
at 21841 matched its own spec throughout. Business decision: skip 21845
entirely rather than generate a second short video per product just for
this slot -- expected to let 21841/21837 persist on their own once 21845's
invalid content is no longer in the payload at all.

Only touches ATTR_VIDEO_LINK/TITLE -- every other attribute, price, and
images are carried through unchanged from the live record (same safe
pattern as push_hashtag_fixes.py).

Not part of daily_run.py -- a one-time catalog push for the already-
generated 175 videos. New products going forward would need their own
video generated + pushed the same way (a follow-up, not handled here).
"""
import json
import os
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import boto3
from dotenv import load_dotenv

from ozon_client import call
from ozon_mapping import extract_article_code_from_offer_id
from r2_storage import R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, upload_file

load_dotenv()

ATTR_VIDEO_LINK = 21841
ATTR_VIDEO_TITLE = 21837
ATTR_VIDEO_COVER = 21845

VIDEOS_DIR = "generated_videos"
PUSH_LOG = "video_push_log.jsonl"
BATCH_SIZE = 100


def find_live_offer_ids_for_articles(article_codes):
    """offer_id -> article_code for every live (non-archived) offer_id whose
    embedded article code is in article_codes, any prefix."""
    wanted = set(article_codes)
    matches = {}
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
            if item.get("archived"):
                continue
            code = extract_article_code_from_offer_id(oid)
            if code in wanted:
                matches[oid] = code
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
    # 21845 (video cover) deliberately omitted -- see module docstring: it
    # actually requires an 8-30s video, not a static image, and including
    # an invalid value there previously dropped the ENTIRE video attribute
    # complex (21841/21837/21845 together), not just the invalid field.
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


def delete_local_video_dir(article_code):
    import shutil
    product_dir = os.path.join(VIDEOS_DIR, article_code)
    if os.path.isdir(product_dir):
        shutil.rmtree(product_dir, ignore_errors=True)


def main():
    if not os.path.isdir(VIDEOS_DIR):
        return print(f"{VIDEOS_DIR}/ not found. Run generate_product_videos.py first.")

    article_codes = [d for d in os.listdir(VIDEOS_DIR) if os.path.isdir(os.path.join(VIDEOS_DIR, d))]

    limit = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--limit="):
        limit = int(sys.argv[1].split("=", 1)[1])
        article_codes = article_codes[:limit]
        print(f"--limit={limit}: testing on the first {len(article_codes)} article code(s) only.\n")

    print(f"{len(article_codes)} article code(s) have a generated video.\n")

    print("Finding live offer_ids for these article codes...")
    offer_to_article = find_live_offer_ids_for_articles(article_codes)
    print(f"{len(offer_to_article)} live offer_id(s) found across those article codes.\n")

    if not offer_to_article:
        return print("Nothing to push.")

    print("Fetching live attributes and prices...")
    records = fetch_attributes(offer_to_article.keys())
    prices = fetch_prices(offer_to_article.keys())

    # Upload each article's video to R2 ONCE, reuse the same URL for every
    # offer_id sharing that article code (color/size variants all get the
    # same video). cover.jpg is generated locally alongside the video but
    # deliberately NOT uploaded/pushed -- see module docstring on 21845.
    article_to_url = {}
    for article_code in article_codes:
        video_path = os.path.join(VIDEOS_DIR, article_code, "video.mp4")
        if not os.path.exists(video_path):
            continue
        try:
            article_to_url[article_code] = upload_file(video_path, f"videos/{article_code}/video.mp4")
        except Exception as e:
            print(f"  ! R2 upload failed for {article_code}: {e}")

    print(f"{len(article_to_url)} article(s) uploaded to R2.\n")

    to_submit = []
    for oid, article_code in offer_to_article.items():
        record = records.get(oid)
        price_info = prices.get(oid)
        video_url = article_to_url.get(article_code)
        if not record or not price_info or not price_info.get("price") or not video_url:
            continue
        item = build_video_item(record, video_url, price_info)
        to_submit.append((oid, article_code, item))

    print(f"{len(to_submit)} item(s) ready to submit.\n")

    total_ok, total_failed = 0, 0
    succeeded_articles = set()
    for i in range(0, len(to_submit), BATCH_SIZE):
        batch = to_submit[i:i + BATCH_SIZE]
        items = [item for _, _, item in batch]
        offer_ids_in_batch = [oid for oid, _, _ in batch]

        print(f"Pushing batch {i // BATCH_SIZE + 1} ({len(items)} item(s))...")
        try:
            result = call("/v3/product/import", {"items": items})
            task_id = result.get("result", {}).get("task_id")
            print(f"  -> task_id={task_id}, waiting for processing...")
            time.sleep(15)

            info = call("/v1/product/import/info", {"task_id": task_id})
            info_items = info.get("result", {}).get("items", [])
            status_by_offer = {it["offer_id"]: it for it in info_items}

            with open(PUSH_LOG, "a", encoding="utf-8") as f:
                for oid, article_code, _ in batch:
                    status_entry = status_by_offer.get(oid, {})
                    ok = status_entry.get("status") == "imported" and not status_entry.get("errors")
                    f.write(json.dumps({
                        "offer_id": oid, "article_code": article_code,
                        "status": status_entry.get("status"), "errors": status_entry.get("errors"),
                    }, ensure_ascii=False) + "\n")
                    if ok:
                        total_ok += 1
                        succeeded_articles.add(article_code)
                    else:
                        total_failed += 1
                        print(f"  FAIL {oid}: {status_entry.get('errors')}")
        except Exception as e:
            print(f"  ! batch failed: {e}")
            total_failed += len(items)

        time.sleep(1)

    print(f"\n{total_ok} offer_id(s) updated successfully, {total_failed} failed.")

    # Clean up local + R2 copies for articles where EVERY offer_id succeeded
    # (business instruction: delete after a successful push, don't keep
    # videos sitting in local storage or R2). An article with any failed
    # offer_id keeps its files so a retry has something to push.
    articles_with_any_failure = {a for oid, a, _ in to_submit
                                  if a not in succeeded_articles}
    fully_succeeded = succeeded_articles - articles_with_any_failure
    print(f"\nCleaning up {len(fully_succeeded)} fully-succeeded article(s) (local + R2)...")
    for article_code in fully_succeeded:
        delete_r2_objects([f"videos/{article_code}/video.mp4"])
        delete_local_video_dir(article_code)

    print("Done.")


if __name__ == "__main__":
    main()
