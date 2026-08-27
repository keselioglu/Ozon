"""
Backfills attribute 8292 ("Объединить на одной карточке" / "Merge on one
card") onto every already-live M&S-family product that's missing it.

New uploads via upload_to_ozon.py already set this attribute correctly (see
MERGE_ATTR_ID in that file) -- every color/size variant of the same M&S
article code gets the same value "mands-{article_code}", so Ozon groups them
onto one PDP with color/size as switchable variants within it. This script
just catches up every product uploaded before that attribute was added to
the upload payload.

Business instruction (2026-08-27, GitHub issue #3): merge by color (default),
keep size as a per-offer variant. Explicitly confirmed NOT to merge across
our different historical offer_id prefixes (MS-, MAR-, SML-, MARKS-, MARK-,
SMLMS-): a live example (SML-MAR-T61004934P vs MARK-T61004104X-style pairs)
turned out to be a duplicate listing Ozon had already rejected/errored on,
not a legitimate color-variant pair -- so the merge key is scoped per prefix
family in addition to per article code. 25 article codes that appear live
under more than one prefix family were found during the audit and are
DELIBERATELY excluded from this backfill (see cross_prefix_candidates.json)
-- those are candidate duplicates/"related similar" groups for manual
business review, not something to auto-merge.

Confirmed live (2026-08-27) via /v1/description-category/attribute for this
category (200001517 / type 93238): 8292 is a required String attribute.
Ozon's own field description: fill it identically across similar products to
get a switcher between them; don't use overly simple values or Type/Brand;
give it a UNIQUE value for anything that should NOT merge. Also confirmed
live: ALL 4,117 live T-coded offer_ids already carry SOME 8292 value from an
earlier, unrelated process, but inconsistently -- different key styles
per-prefix and some truncated article codes -- so this backfill overwrites
to the correct value rather than only filling blanks (see
already_has_merge_key's exact-match check).

Merge key format: "mands-{prefix}{article_code}", matching the prefix family
(e.g. "mands-MS-T81006849L") so only same-prefix, same-article color
variants merge -- consistent with upload_to_ozon.py's own key for new MS-
uploads (which is exactly "mands-{article_code}" with an implicit MS- scope,
since that script only ever uploads under the MS- prefix itself; MS-
uploads' merge key intentionally matches this backfill's MS- key so old and
new MS- listings of the same article still merge together).

Only touches attribute 8292 -- every other attribute, the offer_id, price,
and images are carried through unchanged from the live record (same pattern
as push_hashtag_fixes.py / fix_size_attribute.py).

Not part of daily_run.py -- a one-time catalog-wide backfill.
"""
import json
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from ozon_mapping import extract_article_code_from_offer_id
from upload_to_ozon import MERGE_ATTR_ID

MS_FAMILY_PREFIXES = ("MS-", "MAR-", "SML-", "MARKS-", "MARK-", "SMLMS-")
PUSH_LOG = "merge_backfill_log.jsonl"
BATCH_SIZE = 100


def prefix_of(offer_id):
    for p in sorted(MS_FAMILY_PREFIXES, key=len, reverse=True):
        if offer_id.startswith(p):
            return p
    return None


def merge_key_for(offer_id):
    """Same-prefix, same-article-code color variants share this key.
    Different prefixes for the same article do NOT share a key, so legacy
    duplicate listings stay separate (business decision, issue #3)."""
    prefix = prefix_of(offer_id)
    article_code = extract_article_code_from_offer_id(offer_id)
    if not prefix or not article_code:
        return None
    return f"mands-{prefix}{article_code}"


def find_live_ms_offer_ids():
    """Every live (non-archived) M&S-family offer_id, any prefix."""
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
            if item.get("archived"):
                continue
            if oid.startswith(MS_FAMILY_PREFIXES):
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


def already_has_merge_key(record, merge_key):
    """True only if 8292 is ALREADY exactly the correct value we'd compute --
    not just "already has something". Confirmed live (2026-08-27) that some
    T-coded offer_ids already carry an 8292 value from an earlier, unrelated
    process, and it's sometimes wrong (prefix mismatch, e.g. an SML-MAR-
    offer_id keyed as "MARK-...", or a truncated article code, e.g.
    "MARK-T610049" instead of "MARK-T61004902B") -- those need overwriting,
    not skipping, so an exact-match check is required rather than
    presence-of-any-value."""
    for attr in record.get("attributes", []):
        if attr["id"] == MERGE_ATTR_ID:
            values = attr.get("values", [])
            return any(v.get("value") == merge_key for v in values)
    return False


def build_item_with_merge_key(record, merge_key, price_info):
    attributes = [a for a in record["attributes"] if a["id"] != MERGE_ATTR_ID]
    attributes.append({"id": MERGE_ATTR_ID, "values": [{"value": merge_key}]})

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
    print("Finding live M&S-family offer_ids...")
    offer_ids = find_live_ms_offer_ids()
    print(f"{len(offer_ids)} live M&S-family offer_id(s) found.\n")

    if not offer_ids:
        return print("Nothing to do.")

    print("Fetching live attributes and prices...")
    records = fetch_attributes(offer_ids)
    prices = fetch_prices(offer_ids)

    to_submit = []
    already_ok = []
    unresolvable = []
    for oid in offer_ids:
        record = records.get(oid)
        price_info = prices.get(oid)
        merge_key = merge_key_for(oid)

        if not merge_key:
            unresolvable.append((oid, "could not derive prefix/article code"))
            continue
        if not record or not price_info or not price_info.get("price"):
            unresolvable.append((oid, "missing record/price"))
            continue
        if already_has_merge_key(record, merge_key):
            already_ok.append(oid)
            continue

        item = build_item_with_merge_key(record, merge_key, price_info)
        to_submit.append((oid, merge_key, item))

    print(f"{len(to_submit)} item(s) need the merge attribute backfilled.")
    print(f"{len(already_ok)} already correct (untouched).")
    print(f"{len(unresolvable)} unresolvable.\n")
    if unresolvable:
        print("Unresolvable (need manual attention):")
        for oid, reason in unresolvable[:20]:
            print(f"  {oid}: {reason}")
        if len(unresolvable) > 20:
            print(f"  ... and {len(unresolvable) - 20} more (see console scroll-back)")

    if not to_submit:
        return print("\nNothing to push.")

    total_ok, total_failed = 0, 0
    for i in range(0, len(to_submit), BATCH_SIZE):
        batch = to_submit[i:i + BATCH_SIZE]
        items = [item for _, _, item in batch]
        offer_ids_in_batch = [oid for oid, _, _ in batch]

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

    print(f"\nDone. {total_ok} submitted, {total_failed} failed, {len(already_ok)} already correct, "
          f"{len(unresolvable)} unresolvable.")
    print("Ozon processes /v3/product/import asynchronously — check_upload_status.py can verify task results.")
    print("Ozon's own docs note merging can take up to ~24h to visibly consolidate after import.")


if __name__ == "__main__":
    main()
