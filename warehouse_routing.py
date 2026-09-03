"""
Decides which warehouse a product's stock should live in, and moves it there
when the decision changes (business instruction, 2026-08-27): lightweight,
lower-priced items ship cheaper from the small warehouse than the regular one.

Confirmed live via /v2/warehouse/list (2026-08-27):
  REGULAR_WAREHOUSE_ID = 1020000320456000  "Ozpark Bee Concept"
  SMALL_WAREHOUSE_ID   = 1020002288795000  "Small Items Warehouse_below_500gr"
  (name itself confirms the 500g threshold)

Eligibility rule (business-confirmed): weight < 500g AND current final price
(Ozon's price.price field -- what the buyer actually pays right now,
including any active special-offer/markdown; NOT old_price, which is only
the crossed-out "was" price) < $80 USD. Both conditions must hold; a special
offer that drops the final price under $80 makes an item newly eligible even
if it wasn't before, and price expiring back above $80 makes it ineligible
again -- this needs re-checking on every daily run, not just once at upload.

Scope: M&S-family products only (MS-, MAR-, SML-, MARKS-, MARK-, SMLMS-) --
other brands on this account (H&M, etc.) are out of scope for now, per
business decision, since this pipeline doesn't track their weight/price data.

This module only decides the target warehouse and builds the two-sided
stock write (real count at the target, 0 at the other) -- it doesn't fetch
stock counts itself. Callers (update_stocks.py, refresh_live_stock.py) still
own "what's the real stock count," this owns "which warehouse should it be
in today."

Also writes its own routing decisions to WAREHOUSE_STATE_FILE every run
(business instruction, 2026-09-02: "add number of products in warehouses to
daily report") -- Ozon's stock-read endpoints don't reliably report back
which warehouse currently holds a product's real stock (confirmed live,
2026-09-02: /v4/product/info/stocks' warehouse_ids field came back empty
even for products known to be routed, and the dedicated
stocks-by-warehouse endpoint is obsolete/rejected by the API), so this is
tracked here rather than re-derived from an unreliable read.
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call

REGULAR_WAREHOUSE_ID = 1020000320456000   # "Ozpark Bee Concept"
SMALL_WAREHOUSE_ID = 1020002288795000     # "Small Items Warehouse_below_500gr"
WAREHOUSE_STATE_FILE = "warehouse_assignments.json"

WEIGHT_THRESHOLD_G = 500
PRICE_THRESHOLD_USD = 80.0

MS_FAMILY_PREFIXES = ("MS-", "MAR-", "SML-", "MARKS-", "MARK-", "SMLMS-")


def is_ms_family(offer_id):
    return offer_id.startswith(MS_FAMILY_PREFIXES)


def fetch_weights(offer_ids):
    """offer_id -> weight in grams, via /v4/product/info/attributes, batched
    at 1000 (API max)."""
    offer_ids = list(offer_ids)
    weights = {}
    for i in range(0, len(offer_ids), 1000):
        batch = offer_ids[i:i + 1000]
        cursor = ""
        while True:
            params = {"filter": {"offer_id": batch}, "limit": 1000}
            if cursor:
                params["last_id"] = cursor
            result = call("/v4/product/info/attributes", params)
            for item in result.get("result", []):
                weights[item["offer_id"]] = item.get("weight")
            cursor = result.get("last_id")
            if not cursor or not result.get("result"):
                break
    return weights


def fetch_final_prices(offer_ids):
    """offer_id -> current final price (float, USD) via price.price -- the
    amount the buyer pays right now, already reflecting any active special
    offer. Batched at 1000."""
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
                try:
                    prices[item["offer_id"]] = float(price_info.get("price", 0) or 0)
                except (TypeError, ValueError):
                    prices[item["offer_id"]] = None
            cursor = result.get("cursor")
            if not cursor or not result.get("items"):
                break
    return prices


def decide_target_warehouse(weight_g, final_price_usd):
    """Returns REGULAR_WAREHOUSE_ID or SMALL_WAREHOUSE_ID, or None if either
    input is missing (caller should skip rather than guess a warehouse for
    a product we can't actually evaluate)."""
    if weight_g is None or final_price_usd is None:
        return None
    if weight_g < WEIGHT_THRESHOLD_G and final_price_usd < PRICE_THRESHOLD_USD:
        return SMALL_WAREHOUSE_ID
    return REGULAR_WAREHOUSE_ID


def build_routed_stock_updates(offer_id_to_stock):
    """Given {offer_id: real_stock_count}, returns a list of Ozon
    /v2/products/stocks payload entries that put the real count at the
    correct warehouse and 0 at the other warehouse for every M&S-family
    offer_id, so a daily re-route (e.g. a price drop into a special offer)
    actually moves stock rather than just adding it in a second place.
    Non-M&S-family offer_ids are passed through unchanged, targeting the
    regular warehouse only (out of scope for routing, per business decision).
    Returns (updates, skipped) where skipped lists offer_ids we couldn't
    evaluate (missing weight or price) — those keep whatever warehouse
    assignment they already have; nothing is written for them here."""
    ms_offer_ids = [oid for oid in offer_id_to_stock if is_ms_family(oid)]
    other_offer_ids = [oid for oid in offer_id_to_stock if not is_ms_family(oid)]

    weights = fetch_weights(ms_offer_ids)
    prices = fetch_final_prices(ms_offer_ids)

    updates = []
    skipped = []

    for oid in ms_offer_ids:
        stock = offer_id_to_stock[oid]
        target = decide_target_warehouse(weights.get(oid), prices.get(oid))
        if target is None:
            skipped.append(oid)
            continue
        other = SMALL_WAREHOUSE_ID if target == REGULAR_WAREHOUSE_ID else REGULAR_WAREHOUSE_ID
        updates.append({"offer_id": oid, "stock": stock, "warehouse_id": target})
        updates.append({"offer_id": oid, "stock": 0, "warehouse_id": other})

    for oid in other_offer_ids:
        updates.append({"offer_id": oid, "stock": offer_id_to_stock[oid], "warehouse_id": REGULAR_WAREHOUSE_ID})

    save_warehouse_assignments(updates)
    return updates, skipped


def save_warehouse_assignments(updates):
    """MERGES this run's routing decisions into the persisted state --
    updates only the offer_ids this run actually evaluated, leaving every
    other previously-known offer_id's assignment untouched.

    This MUST merge, not overwrite: build_routed_stock_updates() is called
    from multiple places that each cover a different SUBSET of the catalog
    (update_stocks.py: only today's newly crawled/uploaded rows;
    refresh_live_stock.py: the full known-URL catalog; check_todays_stock.py
    at 6am: only today's newly submitted offer_ids). Confirmed live,
    2026-09-03: an overwrite here silently collapsed the tracked count from
    3,219 (after the 4am/5am full-catalog run) down to 249 (the 6am run's
    much smaller today-only subset), making the daily report's warehouse
    counts wrong for anything not touched that specific day."""
    try:
        with open(WAREHOUSE_STATE_FILE, encoding="utf-8") as f:
            assignments = json.load(f)
    except FileNotFoundError:
        assignments = {}

    for u in updates:
        if u["stock"] > 0:
            assignments[u["offer_id"]] = u["warehouse_id"]

    with open(WAREHOUSE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(assignments, f, ensure_ascii=False, indent=2)


def load_warehouse_assignments():
    """offer_id -> warehouse_id, from the last routing run. Returns {} if
    the state file doesn't exist yet (before the first routing run)."""
    try:
        with open(WAREHOUSE_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
