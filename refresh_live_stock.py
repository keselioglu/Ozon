"""
Daily stock refresh for M&S-sourced products already live on Ozon.

Unlike update_stocks.py (which only pushes stock for rows already sitting in
products.csv from a crawl), this re-fetches each live product's actual M&S page
today and pushes a fresh stock count — covering products this pipeline never
directly crawled (older manual uploads), as long as their M&S source URL is
known.

Covers every offer_id prefix legacy_product_urls.csv has a mapping for (MS-,
MAR-, SML-, MARKS-, MARK- observed live), not just this pipeline's own MS-
convention — the same M&S product line is sold under several different past
manual-upload naming conventions (see upload_to_ozon.py's duplicate-prevention
check), and each of those offer_ids sells real inventory that needs its own
correct stock. legacy_product_urls.csv maps each offer_id directly to its own
M&S URL, so no color/prefix guessing is needed — each offer_id's size is
matched against ITS OWN page independently.

Source of URLs, in priority order:
  1. legacy_product_urls.csv — a direct offer_id -> M&S URL mapping the business
     supplied for products uploaded before this pipeline tracked source URLs.
     Covers multiple prefixes (MS-, MAR-, SML-, MARKS-, MARK-).
  2. products.csv — this pipeline's own crawl history (ms_article_code/parent_sku
     matched against live MS-* offer_ids only, same logic as category_priority.py)
     — this source only ever applies to our own MS- uploads, since only those
     rows carry a recorded crawl URL.

Live offer_ids with no URL in either source at all are left completely
untouched — not modified, not reported as broken (nothing to check them
against). Two other discrepancy cases DO get a stock value written, but it's
always 0 rather than a real re-fetched number, since there's no reliable
source for their actual stock (business decision, 2026-08-26):

  - "wrong URL": the offer_id's mapped URL is verified (via article code/SKU
    — see verify_url_matches_offer_id) to belong to a DIFFERENT product. The
    mapping data itself is wrong and needs correcting — logged separately so
    it's actionable, not just silently zeroed.
  - "unmatched": the page is confirmed correct, but this specific size no
    longer appears on it — treated as effectively sold out.

Stock is routed to the correct warehouse (regular vs. small-items) via
warehouse_routing.py — see that module for the weight/price eligibility rule
(business instruction, 2026-08-27). Routing is re-evaluated every run, not
just once at upload, since a special-offer price change can move a product
in or out of eligibility.
"""
import sys
import time

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from crawler import extract_product
from ozon_client import call
from ozon_mapping import extract_article_code_from_offer_id, extract_eu_size, extract_letter_size
from category_priority import fetch_live_ms_identifiers
from warehouse_routing import build_routed_stock_updates

LEGACY_URL_MAP_FILE = "legacy_product_urls.csv"
PRODUCTS_CSV = "products.csv"
FALLBACK_STOCK = 20
BATCH_SIZE = 100  # Ozon's max per /v2/products/stocks call
REFETCH_LOG = "stock_refresh_skipped.jsonl"
WRONG_URL_LOG = "stock_refresh_wrong_url.jsonl"
UNMATCHED_SUMMARY_FILE = "unmatched_offer_ids.txt"
WRONG_URL_SUMMARY_FILE = "wrong_url_offer_ids.txt"


def load_legacy_url_map(path=LEGACY_URL_MAP_FILE):
    """offer_id -> M&S URL, from the business-supplied mapping. Returns {} if the
    file doesn't exist yet (it's optional)."""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    return dict(zip(df["ID"], df["URL"]))


def fetch_live_offer_ids_matching(candidate_offer_ids):
    """Returns (matching_live_offer_ids, total_live_count) — the subset of
    candidate_offer_ids that are currently live on Ozon (any prefix), plus
    the total live product count from the same pagination pass. Avoids
    assuming a prefix, since legacy_product_urls.csv covers MS-, MAR-,
    SML-, MARKS-, and MARK- offer_ids."""
    candidates = set(candidate_offer_ids)
    live_matches = set()
    total_live = 0
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        total_live += len(items)
        for item in items:
            oid = item.get("offer_id", "")
            if oid in candidates:
                live_matches.add(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return live_matches, total_live


def load_pipeline_url_map(live_offer_ids, path=PRODUCTS_CSV):
    """offer_id -> M&S URL, derived by matching products.csv's ms_article_code/
    parent_sku against live offer_ids (substring match, same as category_priority's
    product_already_live), for products this pipeline itself crawled."""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return {}

    url_map = {}
    for _, row in df.drop_duplicates("ms_article_code").iterrows():
        article_code = row.get("ms_article_code")
        parent_sku = row.get("parent_sku")
        url = row.get("url")
        if pd.isna(url):
            continue
        for oid in live_offer_ids:
            if oid in url_map:
                continue
            if (article_code and str(article_code) in oid) or (parent_sku and str(parent_sku) in oid):
                url_map[oid] = url
    return url_map


def extract_size_token(offer_id):
    """MS-T61004100-KAHVE-34 -> '34', MS-T81006849L-PINKMIX-40 -> '40',
    MAR-T61005100X-46EU -> '46' (legacy MAR- offer_ids append a literal 'EU'
    suffix on numeric sizes — confirmed on real data — which this strips so
    it lines up with the plain EU number extracted from a fresh M&S page).
    The size token is always the last hyphen-separated segment."""
    if "-" not in offer_id:
        return None
    token = offer_id.rsplit("-", 1)[-1]
    if token.endswith("EU") and token[:-2].isdigit():
        return token[:-2]
    return token


def verify_url_matches_offer_id(offer_id, page_article_code, page_parent_sku):
    """Confirms the page we're about to trust for offer_id's stock is actually
    THIS product, not some other product that happens to share a URL mapping
    entry (e.g. a stale/wrong row in legacy_product_urls.csv, or two products
    whose SKUs were transposed when the mapping was compiled). Checks the
    offer_id's own embedded identifier — article code for T-coded products,
    parent_sku for legacy numeric-SKU offer_ids — against what the fresh page
    itself reports. Returns True only when we have a positive match; an
    offer_id with neither identifier resolvable is treated as unverifiable
    (False) rather than trusted by default."""
    offer_article_code = extract_article_code_from_offer_id(offer_id)
    if offer_article_code:
        return page_article_code is not None and str(page_article_code) == offer_article_code

    # Legacy numeric-SKU offer_id (e.g. "MS-10000000601019-S") — the SKU is
    # embedded directly, check it against the page's own parent_sku.
    for token in offer_id.split("-"):
        if token.isdigit() and len(token) >= 9 and page_parent_sku and str(page_parent_sku) == token:
            return True
    return False


def build_stock_updates_for_url(url, offer_ids_for_url):
    """Re-fetches one M&S product page and returns (updates, unmatched,
    wrong_url, warning): updates is [{offer_id, stock}, ...] for every
    offer_id in offer_ids_for_url — always, since both discrepancy cases
    below still get a stock value (0), just via a different path than a
    real page match; unmatched and wrong_url are reported alongside for
    visibility even though their offer_ids are also present in updates.

    unmatched = identity matches but the specific size doesn't appear on the
    page (e.g. a size Ozon still lists that M&S no longer sells in this
    color, confirmed on real data, see MS-T61008800T-ROSEQUARTZ-54).
    wrong_url = the offer_id's embedded article code/SKU does NOT match this
    page at all — the URL mapping entry itself is wrong and needs correcting.

    Business decision (2026-08-26): for both cases there's no reliable stock
    source for that offer_id right now, so it's set to 0 (treated as sold
    out) rather than left at a possibly-stale prior value. The distinction
    still matters operationally: unmatched is usually a discontinued size,
    wrong_url means the mapping data itself needs fixing — hence both stay
    separately logged even though the resulting stock action is the same."""
    try:
        variant_rows = extract_product(url)
    except Exception as e:
        zeroed = [{"offer_id": oid, "stock": 0} for oid in offer_ids_for_url]
        return zeroed, list(offer_ids_for_url), [], f"fetch error: {e}"

    if not variant_rows:
        zeroed = [{"offer_id": oid, "stock": 0} for oid in offer_ids_for_url]
        return zeroed, list(offer_ids_for_url), [], "no product data found on page"

    page_article_code = variant_rows[0].get("ms_article_code")
    page_parent_sku = variant_rows[0].get("parent_sku")

    verified_offer_ids, wrong_url = [], []
    for offer_id in offer_ids_for_url:
        if verify_url_matches_offer_id(offer_id, page_article_code, page_parent_sku):
            verified_offer_ids.append(offer_id)
        else:
            wrong_url.append(offer_id)

    if not verified_offer_ids:
        zeroed = [{"offer_id": oid, "stock": 0} for oid in wrong_url]
        return zeroed, [], wrong_url, (
            f"URL does not match ANY of the {len(offer_ids_for_url)} offer_id(s) mapped to it "
            f"(page is article {page_article_code!r} / SKU {page_parent_sku!r}) — mapping entry needs correcting."
        )

    wrong_url_updates = [{"offer_id": oid, "stock": 0} for oid in wrong_url]
    offer_ids_for_url = verified_offer_ids

    # Match each fresh page variant to an offer_id by its size token. This can't
    # assume the offer_id's trailing token was built via ozon_mapping's UK->RU
    # conversion table — legacy/manually-uploaded offer_ids were confirmed (on
    # real data) to instead use the EU size number M&S displays directly on the
    # page (e.g. label "46 (UK 18)" -> offer_id ends "-46", not "-52" as the
    # UK->RU table would give for UK 18). So we match on BOTH candidates: the
    # label's own leading EU number, and the letter size when present. Whichever
    # one is present on the label is tried; a numeric offer_id token only matches
    # a numeric label EU number, a letter token only matches a letter label.
    fresh_by_size_token = {}
    for v in variant_rows:
        label = (v.get("size_label") or "").strip()
        stock = v.get("stock_count")
        stock = stock if stock is not None else 0

        eu_size = extract_eu_size(label)
        letter_size = extract_letter_size(label)

        if eu_size:
            fresh_by_size_token[eu_size] = stock
        if letter_size:
            fresh_by_size_token[letter_size] = stock

    updates = []
    unmatched = []
    for offer_id in offer_ids_for_url:
        size_token = extract_size_token(offer_id)
        if size_token and size_token.upper() in fresh_by_size_token:
            updates.append({"offer_id": offer_id, "stock": fresh_by_size_token[size_token.upper()]})
        elif size_token and size_token in fresh_by_size_token:
            updates.append({"offer_id": offer_id, "stock": fresh_by_size_token[size_token]})
        else:
            # Right product (identity verified above), but this exact size
            # doesn't appear on the page at all today — treated as sold out /
            # discontinued rather than "unknown", so stock is zeroed rather
            # than left at a possibly-stale prior value (business decision,
            # 2026-08-26). Still logged as unmatched for visibility.
            updates.append({"offer_id": offer_id, "stock": 0})
            unmatched.append(offer_id)

    updates.extend(wrong_url_updates)

    warnings = []
    if unmatched:
        warnings.append(f"{len(unmatched)} offer_id(s) had no matching size on the fresh page — stock set to 0")
    if wrong_url:
        warnings.append(
            f"{len(wrong_url)} offer_id(s) mapped to this URL do NOT belong to it "
            f"(page is article {page_article_code!r} / SKU {page_parent_sku!r}) — mapping entry needs correcting"
        )
    warning = "; ".join(warnings) if warnings else None
    return updates, unmatched, wrong_url, warning


def _key(entry):
    """(offer_id, warehouse_id) — routing can produce two payload rows per
    offer_id (target warehouse + zeroed other warehouse), so matching a
    result back to its request must include warehouse_id, not offer_id alone."""
    return (entry["offer_id"], entry["warehouse_id"])


def push_stock_updates(updates):
    total_ok, total_failed = 0, 0
    retry_queue = []
    for i in range(0, len(updates), BATCH_SIZE):
        batch = updates[i:i + BATCH_SIZE]
        print(f"Updating batch {i // BATCH_SIZE + 1} ({len(batch)} items)...")
        result = call("/v2/products/stocks", {"stocks": batch})
        for r in result.get("result", []):
            key = (r["offer_id"], r["warehouse_id"])
            if r.get("updated"):
                total_ok += 1
            elif any(e.get("code") == "TOO_MANY_REQUESTS" for e in r.get("errors", [])):
                retry_queue.append(next(u for u in batch if _key(u) == key))
            else:
                total_failed += 1
                errors = "; ".join(e.get("message", str(e)) for e in r.get("errors", []))
                print(f"  FAIL {r.get('offer_id')} @ {r.get('warehouse_id')}: {errors}")

    if retry_queue:
        print(f"\n{len(retry_queue)} item(s) hit the per-offer rate limit — waiting 20s and retrying once...")
        time.sleep(20)
        result = call("/v2/products/stocks", {"stocks": retry_queue})
        for r in result.get("result", []):
            if r.get("updated"):
                total_ok += 1
            else:
                total_failed += 1
                errors = "; ".join(e.get("message", str(e)) for e in r.get("errors", []))
                print(f"  FAIL (after retry) {r.get('offer_id')} @ {r.get('warehouse_id')}: {errors}")

    return total_ok, total_failed


def main():
    print("Fetching live MS-* offer_ids from Ozon (for products.csv-derived matching)...")
    live_ms_offer_ids = fetch_live_ms_identifiers()
    print(f"{len(live_ms_offer_ids)} MS-* offer_id(s) live.\n")

    legacy_map = load_legacy_url_map()
    print(f"{len(legacy_map)} offer_id(s) have a known URL via {LEGACY_URL_MAP_FILE} "
          f"(covers MS-, MAR-, SML-, MARKS-, MARK- prefixes).")

    pipeline_map = load_pipeline_url_map(live_ms_offer_ids)
    print(f"{len(pipeline_map)} offer_id(s) have a known URL via {PRODUCTS_CSV} crawl history.\n")

    # legacy map takes priority since it's an exact per-offer_id mapping;
    # pipeline map fills in anything legacy didn't cover. Confirm each
    # candidate is actually still live before including it — legacy_product_urls.csv
    # isn't prefix-filtered, so this is the only place scope narrows to "live now".
    url_for_offer_id = {**pipeline_map, **legacy_map}
    print("Confirming which of those offer_ids are actually live right now...")
    known_offer_ids, total_live = fetch_live_offer_ids_matching(url_for_offer_id.keys())
    unknown_count = total_live - len(known_offer_ids)

    print(f"{total_live} product(s) live on the account in total.")
    print(f"{len(known_offer_ids)} of them have a resolvable M&S URL — will be refreshed today.")
    print(f"{unknown_count} have no known source URL — left untouched.\n")

    if not known_offer_ids:
        return print("Nothing to refresh.")

    # Group offer_ids by URL so each product page is only fetched once.
    offer_ids_by_url = {}
    for oid in known_offer_ids:
        url = url_for_offer_id[oid]
        offer_ids_by_url.setdefault(url, []).append(oid)

    print(f"{len(offer_ids_by_url)} distinct product page(s) to re-fetch.\n")

    all_updates = []
    all_unmatched = []
    all_wrong_url = []
    fetch_failures = 0
    for i, (url, offer_ids) in enumerate(offer_ids_by_url.items(), 1):
        print(f"[{i}/{len(offer_ids_by_url)}] {url} ({len(offer_ids)} offer_id(s))")
        updates, unmatched, wrong_url, warning = build_stock_updates_for_url(url, offer_ids)
        if warning:
            print(f"  ! {warning}")
            import json
            if unmatched or wrong_url:
                with open(REFETCH_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "url": url, "offer_ids": offer_ids, "warning": warning,
                        "unmatched_offer_ids": unmatched,
                    }, ensure_ascii=False) + "\n")
            if wrong_url:
                with open(WRONG_URL_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "url": url, "wrong_offer_ids": wrong_url,
                    }, ensure_ascii=False) + "\n")
        if not updates and warning and "fetch error" in warning:
            fetch_failures += 1
        all_updates.extend(updates)
        all_unmatched.extend(unmatched)
        all_wrong_url.extend(wrong_url)
        time.sleep(1.2)  # be polite to M&S, same pacing as crawler.py

    print(f"\n{len(all_updates)} stock update(s) ready to push ({fetch_failures} page(s) failed to fetch entirely).\n")

    if all_wrong_url:
        print(f"{len(all_wrong_url)} offer_id(s) are mapped to a URL that does NOT belong to them — "
              f"their stock was set to 0 (business decision: no reliable source for their real stock, so "
              f"treat as sold out rather than leave a possibly-stale value). This means "
              f"legacy_product_urls.csv (or the crawl-derived mapping) has a wrong entry for these — "
              f"the mapping itself still needs correcting. Full list in {WRONG_URL_LOG}.\n")
        with open(WRONG_URL_SUMMARY_FILE, "w", encoding="utf-8") as f:
            for oid in sorted(set(all_wrong_url)):
                f.write(oid + "\n")

    if all_unmatched:
        print(f"{len(all_unmatched)} live offer_id(s) had NO matching size on their M&S page today — "
              f"their stock was set to 0 (business decision: treat as sold out/discontinued rather than "
              f"leaving a possibly-stale value). This usually means the offer_id represents a size/color "
              f"combination M&S no longer sells. Full list in {UNMATCHED_SUMMARY_FILE}.\n")
        with open(UNMATCHED_SUMMARY_FILE, "w", encoding="utf-8") as f:
            for oid in sorted(set(all_unmatched)):
                f.write(oid + "\n")

    if not all_updates:
        return print("Nothing to push.")

    print("Deciding target warehouse per product (weight/price routing)...")
    offer_id_to_stock = {u["offer_id"]: u["stock"] for u in all_updates}
    routed_updates, routing_skipped = build_routed_stock_updates(offer_id_to_stock)
    if routing_skipped:
        print(f"{len(routing_skipped)} offer_id(s) skipped for routing (missing weight/price) — "
              f"stock not pushed for these; existing warehouse assignment left as-is.")
    print(f"{len(routed_updates)} warehouse-routed stock update(s) to push.\n")

    total_ok, total_failed = push_stock_updates(routed_updates)
    print(f"\nDone. {total_ok} updated, {total_failed} failed, {unknown_count} skipped (no known URL).")


if __name__ == "__main__":
    main()
