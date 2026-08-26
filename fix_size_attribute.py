"""
One-off correction: fixes the size ATTRIBUTE (Ozon dictionary attribute 4295,
shown on the buyer-facing PDP) for every live MS-* product with a numeric size,
without touching the offer_id, SKU, price, images, or anything else.

Root cause (found 2026-08-26, investigating a stock-discrepancy report on
MS-T61008800T-ROSEQUARTZ-40): build_sku() in upload_to_ozon.py was using
map_size_to_ozon()'s RU dictionary value as the number embedded in every
offer_id, but every listing on this account (including this pipeline's own
prior uploads) actually uses the raw EU number M&S displays on its page as
the offer_id number. The offer_id number itself was therefore always correct
(it matches real inventory), but the size ATTRIBUTE submitted alongside it
was for the WRONG size — e.g. offer_id "...-40" (EU 40, UK 12) was carrying
the RU attribute value for EU 40's OWN "UK 12 -> RU 46" conversion... except
the code passed ru_size into build_sku, so this specific offer_id actually
got created with the attribute for a DIFFERENT UK size (see PR/commit history
for the exact mechanism). Confirmed live via /v4/product/info/attributes:
MS-T61008800T-ROSEQUARTZ-40 carried attribute value "40" (dictionary_value_id
35535, i.e. RU 40) when the correct value for its EU-40 offer_id is RU 46
(dictionary_value_id 35429).

Fix approach (chosen by the business over archiving/recreating, since the
offer_id and underlying inventory were never wrong): derive the correct RU
size from each offer_id's own EU number (via a reverse lookup through
ozon_mapping's UK<->EU<->RU chart), and if it differs from the live
attribute, resubmit that one product via /v3/product/import with everything
else unchanged except the size attribute.

This is NOT part of daily_run.py — a live catalog correction of this scale
is always a deliberate, by-hand action, run once. Every correction is logged
to fixed_size_attribute_log.jsonl (gitignored) before the API call.
"""
import json
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from ozon_mapping import ATTR_SIZE, UK_TO_EU_SIZE, UK_TO_RU_SIZE, _load_size_dict

FIX_LOG = "fixed_size_attribute_log.jsonl"
BATCH_SIZE = 100  # /v4/product/info/attributes and /v3/product/import both accept up to 1000, but keep batches manageable

# Reverse: EU size string -> correct RU size string, derived from the two
# chart-based tables in ozon_mapping.py (both confirmed against the business's
# chart, 2026-08-26).
EU_TO_UK_SIZE = {eu: uk for uk, eu in UK_TO_EU_SIZE.items()}
EU_TO_RU_SIZE = {eu: UK_TO_RU_SIZE[uk] for eu, uk in EU_TO_UK_SIZE.items() if uk in UK_TO_RU_SIZE}


def extract_eu_from_offer_id(offer_id):
    """MS-T61008800T-ROSEQUARTZ-40 -> '40'. The offer_id's trailing numeric
    token, which is confirmed (see module docstring) to always be the EU size
    for every numeric-size listing on this account. Returns None for
    letter-size offer_ids (...-M, ...-XL) or non-numeric-size products."""
    if "-" not in offer_id:
        return None
    token = offer_id.rsplit("-", 1)[-1]
    return token if token.isdigit() else None


def find_live_ms_numeric_size_offer_ids():
    """Returns the set of live MS-* offer_ids whose trailing token is a
    numeric EU size (excludes letter sizes, which were never affected by
    this bug — see products.csv cross-check in conversation history)."""
    matches = set()
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
            if oid.startswith("MS-") and not item.get("archived") and extract_eu_from_offer_id(oid):
                matches.add(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return matches


def fetch_prices(offer_ids):
    """Current live price/currency/vat for these offer_ids, batched at 1000 —
    /v3/product/import requires price on every submitted item (confirmed live:
    omitting it defaults to an invalid negative price and the import fails)."""
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


def fetch_attributes(offer_ids):
    """Full /v4/product/info/attributes records for these offer_ids, batched at 1000."""
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


def find_mismatches(offer_ids):
    """Returns [(offer_id, record, correct_ru_size, correct_dict_value_id, price_info), ...]
    for every offer_id whose live size attribute doesn't match what its own
    EU number implies. Skips (does not guess) any offer_id whose EU number
    isn't in the confirmed chart, whose attribute record is missing the size
    attribute entirely, or whose price couldn't be fetched — those need
    separate manual review."""
    size_dict = _load_size_dict()
    records = fetch_attributes(offer_ids)
    prices = fetch_prices(offer_ids)
    mismatches = []
    unresolvable = []

    for oid in offer_ids:
        eu = extract_eu_from_offer_id(oid)
        correct_ru = EU_TO_RU_SIZE.get(eu)
        if not correct_ru:
            unresolvable.append((oid, f"EU size {eu!r} not in the confirmed chart"))
            continue
        correct_dict_id = size_dict.get(correct_ru)
        if not correct_dict_id:
            unresolvable.append((oid, f"RU size {correct_ru!r} not in Ozon's live size dictionary"))
            continue

        record = records.get(oid)
        if not record:
            unresolvable.append((oid, "no attribute record returned by /v4/product/info/attributes"))
            continue

        size_attr = next((a for a in record["attributes"] if a["id"] == ATTR_SIZE), None)
        if not size_attr or not size_attr.get("values"):
            unresolvable.append((oid, "no size attribute present on the live record"))
            continue

        current_dict_id = size_attr["values"][0].get("dictionary_value_id")
        if current_dict_id == correct_dict_id:
            continue

        price_info = prices.get(oid)
        if not price_info or not price_info.get("price"):
            unresolvable.append((oid, "no live price found — cannot safely resubmit without one"))
            continue

        mismatches.append((oid, record, correct_ru, correct_dict_id, price_info))

    return mismatches, unresolvable


def build_corrected_item(record, correct_dict_id, price_info):
    """Rebuilds a /v3/product/import item from a live attribute record,
    replacing ONLY the size attribute's dictionary_value_id. Everything else
    (name, images, other attributes, dimensions, category, price) is passed
    through unchanged from what's already live. price_info is required —
    confirmed live that omitting it makes the import default to an invalid
    negative price and fail (see module docstring test note)."""
    attributes = []
    for attr in record["attributes"]:
        if attr["id"] == ATTR_SIZE:
            attributes.append({"id": ATTR_SIZE, "values": [{"dictionary_value_id": correct_dict_id}]})
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
        # Remaining required fields /v3/product/import needs that aren't
        # size-related — passed through from the live record unchanged.
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
    print("Finding live MS-* offer_ids with a numeric size...")
    offer_ids = find_live_ms_numeric_size_offer_ids()
    print(f"{len(offer_ids)} candidate(s) found.\n")

    print("Checking each one's live size attribute against its offer_id's own EU number...")
    mismatches, unresolvable = find_mismatches(offer_ids)
    print(f"{len(mismatches)} mismatched (will be corrected), {len(unresolvable)} unresolvable (skipped, see log).\n")

    if unresolvable:
        with open(FIX_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "unresolvable", "items": unresolvable}, ensure_ascii=False) + "\n")

    if not mismatches:
        return print("Nothing to fix.")

    total_ok, total_failed = 0, 0
    for i in range(0, len(mismatches), BATCH_SIZE):
        batch = mismatches[i:i + BATCH_SIZE]
        items = [build_corrected_item(record, correct_dict_id, price_info)
                 for _, record, _, correct_dict_id, price_info in batch]

        with open(FIX_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "action": "correction_batch_started",
                "corrections": [
                    {"offer_id": oid, "correct_ru_size": ru, "correct_dictionary_value_id": did}
                    for oid, _, ru, did, _ in batch
                ],
            }, ensure_ascii=False) + "\n")

        print(f"Correcting batch {i // BATCH_SIZE + 1} ({len(items)} product(s))...")
        try:
            result = call("/v3/product/import", {"items": items})
            task_id = result.get("result", {}).get("task_id")
            print(f"  -> task_id={task_id}")
            with open(FIX_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"action": "correction_batch_submitted", "task_id": task_id,
                                     "offer_ids": [oid for oid, *_ in batch]}, ensure_ascii=False) + "\n")
            total_ok += len(items)
        except Exception as e:
            print(f"  ! batch failed: {e}")
            total_failed += len(items)
            with open(FIX_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"action": "correction_batch_failed", "error": str(e),
                                     "offer_ids": [oid for oid, *_ in batch]}, ensure_ascii=False) + "\n")

        time.sleep(1)

    print(f"\nDone. {total_ok} submitted for correction, {total_failed} failed.")
    print("Ozon processes /v3/product/import asynchronously — check_upload_status.py can verify task results.")
    print(f"Full record in {FIX_LOG}.")


if __name__ == "__main__":
    main()
