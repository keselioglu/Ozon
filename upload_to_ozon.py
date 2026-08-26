"""
Reads products.csv (from crawler.py) and uploads each size variant to Ozon
as a product under Clothing > Underwear > Underwear Trunks/Panties(/Set).

Every mapping decision (size, color) is logged to mapping_log.jsonl for audit,
even though this runs fully automatically without pausing for confirmation.
"""
import json
import os
import sys
import time

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from ozon_mapping import (
    ATTR_BRAND, ATTR_COLOR, ATTR_GENDER, ATTR_SIZE, BRAND_MARKS_AND_SPENCER_ID,
    GENDER_FEMALE_ID, extract_article_code_from_offer_id, log_mapping_decision,
    map_color_to_ozon, map_size_to_eu, map_size_to_ozon, resolve_category_and_type,
)
from ozon_translations import BRAND_PREFIX, COLLECTION_ID, WARRANTY_ID, get_translation

ATTR_MATERIAL = 4496
ATTR_PLANTING_TYPE = 4619
ATTR_CARE = 4655
ATTR_ANNOTATION = 4191
ATTR_MATERIAL_COMPOSITION = 4604
ATTR_COLLECTION = 4503
ATTR_WARRANTY = 10400
ATTR_HASHTAGS = 23171

PRODUCTS_CSV = "products.csv"
MAPPING_LOG = "mapping_log.jsonl"
SKIPPED_LOG = "upload_skipped.jsonl"
TASK_LOG = "upload_tasks.jsonl"
DEFERRED_LOG = "deferred_items.json"

TRY_TO_USD_RATE = 0.083
MERGE_ATTR_ID = 8292  # "Merge on One PDP" — grouping key so size/color variants share one product page

# Package weight/dims are not provided by M&S's page; these are reasonable estimates
# for folded underwear in a poly bag, scaled by pack count. Spot-check after first listing.
PACK_COUNT_RE_KEYWORDS = {"5'li": 5, "3'lü": 3}
SINGLE_ITEM_DIMS = {"weight": 100, "depth": 15, "width": 12, "height": 2}   # grams, cm
PER_EXTRA_ITEM_WEIGHT = 80  # grams added per additional item in a multi-pack

SET_KEYWORDS = ("'lü", "'li", "set", "seti")  # Turkish multi-pack naming: "3'lü ... Seti"


def is_multi_pack(name):
    lower = (name or "").lower()
    return any(kw in lower for kw in SET_KEYWORDS)


def pack_count(name):
    lower = (name or "").lower()
    for keyword, count in PACK_COUNT_RE_KEYWORDS.items():
        if keyword in lower:
            return count
    return 1


def package_dimensions(name):
    count = pack_count(name)
    dims = dict(SINGLE_ITEM_DIMS)
    if count > 1:
        dims["weight"] += PER_EXTRA_ITEM_WEIGHT * (count - 1)
        dims["depth"] += 2 * (count - 1)  # packs are a bit bulkier, not linearly taller
    return dims


def build_sku(article_code, color, ru_size):
    """MS-{article_code}-{color, no spaces}-{ru_size} — the business's own SKU convention."""
    color_token = (color or "").replace(" ", "").upper()
    return f"MS-{article_code}-{color_token}-{ru_size}"


def build_ozon_item(row, offer_id_suffix=""):
    """Transforms one crawled variant row into an Ozon /v3/product/import item.
    Returns (item_dict, warnings_list) — item_dict is None if the row should not be uploaded
    (out of stock, or a required field couldn't be mapped)."""
    warnings = []
    article_code = row.get("ms_article_code")
    if pd.isna(article_code) or not article_code:
        return None, ["Missing M&S article code (data-unique-product-id not found on page) — product not built."]

    stock_count = row.get("stock_count")
    if pd.isna(stock_count) or int(stock_count) <= 0:
        return None, ["Zero/unknown stock — not pushed to Ozon."]

    size_id, ru_size, size_warning = map_size_to_ozon(row.get("size_label"))
    if size_warning:
        warnings.append(f"size: {size_warning}")
    log_mapping_decision(MAPPING_LOG, article_code, "size", row.get("size_label"), ru_size, size_warning)

    # eu_size is the number embedded in the offer_id (matches every legacy
    # listing on this account) — distinct from ru_size, which is the Ozon
    # size ATTRIBUTE value shown on the PDP. Using ru_size for the offer_id
    # was a real bug: it silently mislabeled every numeric-size product's SKU
    # with the wrong physical size (e.g. UK 6 uploaded as offer_id "...-40",
    # the EU number for UK 12 — confirmed on T61008800T, 2026-08-26).
    eu_size, eu_size_warning = map_size_to_eu(row.get("size_label"))
    if eu_size_warning:
        warnings.append(f"eu_size: {eu_size_warning}")
    log_mapping_decision(MAPPING_LOG, article_code, "eu_size", row.get("size_label"), eu_size, eu_size_warning)

    color_id, matched_color, color_warning = map_color_to_ozon(row.get("color"))
    if color_warning:
        warnings.append(f"color: {color_warning}")
    log_mapping_decision(MAPPING_LOG, article_code, "color", row.get("color"), matched_color, color_warning)

    if not size_id or not color_id or not eu_size:
        return None, warnings + ["Missing required size (RU attribute, EU offer_id number) or color mapping — product not built."]

    category_id, type_id = resolve_category_and_type(row.get("name"), is_multi_pack(row.get("name")))
    if not category_id:
        return None, warnings + [
            f"Could not determine Ozon category from product name {row.get('name')!r} "
            "(no 'kulot'/'atlet' keyword match) — product not built."
        ]

    translation = get_translation(article_code)
    if not translation:
        return None, warnings + [
            f"No Russian translation on file for article {article_code} — "
            "Ozon rejects Latin-character names, so this product was not built. "
            "Add an entry to ozon_translations.py to include it."
        ]

    sku = build_sku(article_code, row.get("color"), eu_size)

    try_price = row.get("price")
    if pd.isna(try_price):
        return None, warnings + ["Missing price — product not built."]
    usd_price = round(float(try_price) * TRY_TO_USD_RATE, 2)

    dims = package_dimensions(row.get("name"))

    images = [u.strip() for u in str(row.get("image_urls") or "").split("|") if u.strip()]

    attributes = [
        {"id": ATTR_SIZE, "values": [{"dictionary_value_id": size_id}]},
        {"id": ATTR_COLOR, "values": [{"dictionary_value_id": color_id}]},
        {"id": ATTR_GENDER, "values": [{"dictionary_value_id": GENDER_FEMALE_ID}]},
        {"id": ATTR_BRAND, "values": [{"dictionary_value_id": BRAND_MARKS_AND_SPENCER_ID}]},
        {"id": MERGE_ATTR_ID, "values": [{"value": f"mands-{article_code}"}]},
        {"id": ATTR_CARE, "values": [{"value": translation["care_text"]}]},
        {"id": ATTR_ANNOTATION, "values": [{"value": translation["description"][:500]}]},
        {"id": ATTR_MATERIAL_COMPOSITION, "values": [{"value": translation["material_composition"]}]},
        {"id": ATTR_COLLECTION, "values": [{"dictionary_value_id": COLLECTION_ID}]},
        {"id": ATTR_WARRANTY, "values": [{"dictionary_value_id": WARRANTY_ID}]},
        {"id": ATTR_HASHTAGS, "values": [{"value": translation["hashtags"]}]},
    ]
    if translation.get("material_id"):
        attributes.append({"id": ATTR_MATERIAL, "values": [{"dictionary_value_id": translation["material_id"]}]})
    if translation.get("planting_type_id"):
        attributes.append({"id": ATTR_PLANTING_TYPE, "values": [{"dictionary_value_id": translation["planting_type_id"]}]})

    item = {
        "offer_id": f"{sku}{offer_id_suffix}",
        "name": BRAND_PREFIX + translation["name"],
        "description_category_id": category_id,
        "type_id": type_id,
        "price": str(usd_price),
        "currency_code": "USD",
        "weight": dims["weight"],
        "weight_unit": "g",
        "depth": dims["depth"],
        "width": dims["width"],
        "height": dims["height"],
        "dimension_unit": "cm",
        "images": images,
        "primary_image": images[0] if images else None,
        "attributes": attributes,
        "vat": "0",
    }
    return item, warnings


def check_quota():
    """Reads live daily_create/daily_update limits. Returns dict or None if the call fails."""
    try:
        return call("/v4/product/info/limit", {})
    except Exception as e:
        print(f"WARNING: could not check upload quota ({e}) — proceeding without a pre-flight check.")
        return None


def find_existing_offer_ids(offer_ids):
    """Checks which of these offer_ids already exist on Ozon (updates, not creates —
    don't count against daily_create quota). Queries in batches of 1000 (API max)."""
    existing = set()
    offer_ids = list(offer_ids)
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
                existing.add(item["offer_id"])
            cursor = page.get("last_id")
            if not cursor or not page.get("items"):
                break
    return existing


def fetch_live_article_codes_by_prefix():
    """Scans EVERY live offer_id on the account (not just our own MS- ones) and
    returns {article_code: set_of_offer_id_prefixes_it_appears_under}, using
    extract_article_code_from_offer_id to find the M&S article code wherever
    it's embedded, regardless of naming convention. Used to skip creating a
    new MS- listing for a product already sold under a different prefix
    (MAR-, SML-MAR-, SMLMS-, etc.)."""
    codes_to_prefixes = {}
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
            code = extract_article_code_from_offer_id(oid)
            if code:
                prefix = oid.split("-", 1)[0]
                codes_to_prefixes.setdefault(code, set()).add(prefix)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return codes_to_prefixes


def load_deferred_items():
    try:
        with open(DEFERRED_LOG, "r", encoding="utf-8") as f:
            items = json.load(f)
        os.remove(DEFERRED_LOG)  # consumed — today's run will re-defer whatever still doesn't fit
        return items
    except FileNotFoundError:
        return []


def main():
    deferred_items = load_deferred_items()
    if deferred_items:
        print(f"Resuming {len(deferred_items)} item(s) deferred from a previous run (quota-capped).\n")

    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found. Run crawler.py first.")

    print(f"{len(df)} variant rows loaded from {PRODUCTS_CSV}\n")

    print("Checking live catalog for this article code under any other offer_id naming convention...")
    live_article_codes = fetch_live_article_codes_by_prefix()
    print(f"{len(live_article_codes)} distinct M&S article code(s) found live across the whole account.\n")

    items = []
    skipped = 0
    for _, row in df.iterrows():
        article_code = row.get("ms_article_code")
        live_prefixes = live_article_codes.get(article_code, set())
        if live_prefixes - {"MS"}:
            skipped += 1
            reason = (
                f"Article {article_code} is already live under a different offer_id prefix "
                f"({', '.join(sorted(live_prefixes - {'MS'}))}) — skipping to avoid a duplicate listing, "
                "even though the color/size may differ."
            )
            with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "sku": row.get("variant_sku"), "name": row.get("name"),
                    "reasons": [reason],
                }, ensure_ascii=False) + "\n")
            print(f"SKIP {row.get('variant_sku')} ({row.get('name')}): {reason}")
            continue

        item, warnings = build_ozon_item(row)
        if item is None:
            skipped += 1
            with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "sku": row.get("variant_sku"), "name": row.get("name"),
                    "reasons": warnings,
                }, ensure_ascii=False) + "\n")
            print(f"SKIP {row.get('variant_sku')} ({row.get('name')}): {'; '.join(warnings)}")
            continue
        if warnings:
            print(f"WARN {row.get('variant_sku')}: {'; '.join(warnings)}")
        items.append(item)

    print(f"\n{len(items)} item(s) ready to upload, {skipped} skipped (see {SKIPPED_LOG}).\n")

    if not items:
        return print("Nothing to upload.")

    if deferred_items:
        # Deferred offer_ids take priority so a previous run's quota-capped items go
        # first this time, rather than risk being deferred again behind newer ones.
        deferred_offer_ids = {it["offer_id"] for it in deferred_items}
        items.sort(key=lambda it: it["offer_id"] not in deferred_offer_ids)

    print("Checking which offer_ids already exist on Ozon (updates don't count against daily_create quota)...")
    existing_offer_ids = find_existing_offer_ids(it["offer_id"] for it in items)
    new_items = [it for it in items if it["offer_id"] not in existing_offer_ids]
    update_items = [it for it in items if it["offer_id"] in existing_offer_ids]
    print(f"  {len(new_items)} new product(s) to create, {len(update_items)} existing to update.\n")

    quota = check_quota()
    deferred = []
    if quota:
        daily_create = quota.get("daily_create", {})
        remaining = daily_create.get("limit", 0) - daily_create.get("usage", 0)
        print(f"Daily create quota: {daily_create.get('usage')}/{daily_create.get('limit')} used, "
              f"{remaining} remaining (resets {daily_create.get('reset_at')}).")
        if len(new_items) > remaining:
            deferred = new_items[remaining:]
            new_items = new_items[:remaining]
            print(f"Capping today's new-product uploads at {remaining} (quota). "
                  f"{len(deferred)} deferred to {DEFERRED_LOG} — re-run after the quota resets.\n")

    to_upload = update_items + new_items

    if not to_upload:
        print("Nothing within quota to upload right now.")
    else:
        # Ozon accepts up to 1000 items per call; batch at 100 to keep responses manageable.
        BATCH_SIZE = 100
        for i in range(0, len(to_upload), BATCH_SIZE):
            batch = to_upload[i:i + BATCH_SIZE]
            print(f"Uploading batch {i // BATCH_SIZE + 1} ({len(batch)} items)...")
            result = call("/v3/product/import", {"items": batch})
            task_id = result.get("result", {}).get("task_id")
            print(f"  -> task_id={task_id}")
            with open(TASK_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"task_id": task_id, "batch_size": len(batch)}) + "\n")
            time.sleep(1)

        print(f"\nUpload requests submitted for {len(to_upload)} item(s). Check status with check_upload_status.py")
        print(f"(Ozon processes asynchronously — task_ids logged to {TASK_LOG})")

    if deferred:
        with open(DEFERRED_LOG, "w", encoding="utf-8") as f:
            json.dump(deferred, f, ensure_ascii=False)
        print(f"\n{len(deferred)} item(s) saved to {DEFERRED_LOG} for tomorrow's run.")


if __name__ == "__main__":
    main()
