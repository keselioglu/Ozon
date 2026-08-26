"""
Pushes the corrected hashtags/description from ozon_translations.py (see
fix_hashtags.py) to every live Ozon offer_id, for every article code whose
translation entry was rewritten. Fixing the source file doesn't change
anything live on its own -- Ozon only reflects what was last submitted via
/v3/product/import, so each affected listing needs its attributes (23171
hashtags, 4191 annotation/description) resubmitted.

Only touches ATTR_HASHTAGS and ATTR_ANNOTATION -- every other attribute,
the offer_id, price, and images are carried through unchanged from the
live record (same pattern as fix_size_attribute.py, including the price
field, which is required on every /v3/product/import submission or Ozon
defaults it to an invalid negative value and rejects the whole item).

Not part of daily_run.py -- a one-time push after a hashtag-format cleanup.
"""
import json
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from ozon_mapping import extract_article_code_from_offer_id
from ozon_translations import PRODUCT_TRANSLATIONS

ATTR_HASHTAGS = 23171
ATTR_ANNOTATION = 4191
PUSH_LOG = "hashtag_push_log.jsonl"
BATCH_SIZE = 100


def find_live_offer_ids_for_articles(article_codes):
    """offer_id -> article_code for every live offer_id whose embedded
    article code is in article_codes (any prefix — MS-, MAR-, SML-, etc.,
    same reasoning as upload_to_ozon.py's duplicate check: the same M&S
    product is sold under several naming conventions on this account, and
    all of them should get the corrected hashtags/description)."""
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


def build_corrected_item(record, new_hashtags, new_description, price_info):
    attributes = []
    for attr in record["attributes"]:
        if attr["id"] == ATTR_HASHTAGS:
            attributes.append({"id": ATTR_HASHTAGS, "values": [{"value": new_hashtags}]})
        elif attr["id"] == ATTR_ANNOTATION:
            attributes.append({"id": ATTR_ANNOTATION, "values": [{"value": new_description[:500]}]})
        else:
            attributes.append(attr)

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


def main():
    article_codes = list(PRODUCT_TRANSLATIONS.keys())
    print(f"Finding live offer_ids for {len(article_codes)} article code(s)...")
    offer_to_article = find_live_offer_ids_for_articles(article_codes)
    print(f"{len(offer_to_article)} live offer_id(s) found.\n")

    if not offer_to_article:
        return print("Nothing to push.")

    print("Fetching live attributes and prices...")
    records = fetch_attributes(offer_to_article.keys())
    prices = fetch_prices(offer_to_article.keys())

    to_submit = []
    unresolvable = []
    for oid, article_code in offer_to_article.items():
        translation = PRODUCT_TRANSLATIONS.get(article_code)
        record = records.get(oid)
        price_info = prices.get(oid)
        if not translation or not record or not price_info or not price_info.get("price"):
            unresolvable.append((oid, "missing translation/record/price"))
            continue
        item = build_corrected_item(record, translation["hashtags"], translation["description"], price_info)
        to_submit.append((oid, item))

    print(f"{len(to_submit)} item(s) ready to submit, {len(unresolvable)} unresolvable.\n")

    total_ok, total_failed = 0, 0
    for i in range(0, len(to_submit), BATCH_SIZE):
        batch = to_submit[i:i + BATCH_SIZE]
        items = [item for _, item in batch]
        offer_ids_in_batch = [oid for oid, _ in batch]

        with open(PUSH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "push_batch_started", "offer_ids": offer_ids_in_batch}, ensure_ascii=False) + "\n")

        print(f"Pushing batch {i // BATCH_SIZE + 1} ({len(items)} item(s))...")
        try:
            result = call("/v3/product/import", {"items": items})
            task_id = result.get("result", {}).get("task_id")
            print(f"  -> task_id={task_id}")
            with open(PUSH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"action": "push_batch_submitted", "task_id": task_id,
                                     "offer_ids": offer_ids_in_batch}, ensure_ascii=False) + "\n")
            total_ok += len(items)
        except Exception as e:
            print(f"  ! batch failed: {e}")
            total_failed += len(items)
            with open(PUSH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"action": "push_batch_failed", "error": str(e),
                                     "offer_ids": offer_ids_in_batch}, ensure_ascii=False) + "\n")

        time.sleep(1)

    print(f"\nDone. {total_ok} submitted, {total_failed} failed, {len(unresolvable)} unresolvable.")
    print("Ozon processes /v3/product/import asynchronously — check_upload_status.py can verify task results.")


if __name__ == "__main__":
    main()
