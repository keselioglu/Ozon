"""
Cyclical, priority-ordered category discovery. Unlike crawler.py's
run_category_discovery() (which marks a category "done" forever once
processed), this walks category_priority.csv in priority order every day,
starting from priority 1, queuing products not yet live on Ozon until the
day's PLAN (daily_plan.py, default 500 new offers, split per category by
the owner's allocations) is covered -- counted in estimated offers, aiming
DISCOVERY_OVERSHOOT_FACTOR over -- or every category has been checked. The
next run starts back at priority 1.

"Already live on Ozon" is checked two ways, since the account has two upload
eras with different offer_id conventions:
  - legacy/manual uploads use the Turkish site's numeric SKU (parent_sku)
  - this pipeline's own uploads use MS-{ms_article_code}-{color}-{size}
A product only counts as "new" if neither its ms_article_code nor its
parent_sku appears in any live MS-* offer_id on the account.

Only category types the pipeline can actually map/translate are considered
-- is_supported_category() delegates to ozon_mapping.is_known_product_type(),
the SAME keyword groups resolve_category_and_type() itself uses to build
the upload, so the two can't drift out of sync (they did, silently, for a
while -- see is_supported_category()'s own docstring). Any other category
name in the priority file is skipped automatically without being crawled —
no crawl requests are spent on categories nothing downstream can list.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from crawler import discover_category_products, load_line_set
from ozon_client import call
from ozon_mapping import is_known_product_type
import daily_plan

PRIORITY_FILE = "category_priority.csv"
URLS_FILE = "product_urls.txt"
INVENTORY_FILE = "category_inventory.json"  # shared with category_inventory.py
BRAND = "M&S"  # the only brand this discovery walks

# Discovery queues this much MORE than the plan asks for (in estimated
# offers), because upload-time color-dedup / duplicate / size checks drop
# some. The upload is the hard cap; overflow goes to deferred_items.json.
DISCOVERY_OVERSHOOT_FACTOR = 1.2


# "Umbrella" category pages whose own CATEGORY NAME is too generic to match
# any type keyword (e.g. "Giyim (Tümü)" = "Clothing (All)"), but whose
# CONTENTS are a mix of many product types, several of which the pipeline
# does support. is_supported_category() checks the name only (a deliberate
# design choice -- see its own docstring -- to avoid spending crawl requests
# on categories with nothing minable), which silently excluded these pages
# entirely: confirmed live, 2026-09-10, business instruction "add 2000 new
# products from kadin-giyim" found kadin-giyim never actually crawled because
# its name never matched a keyword, even though most of its individual
# products do. Listed explicitly by URL (not name) since the name itself
# isn't a reliable signal here by construction -- unresolvable products
# inside these pages are still correctly skipped downstream, per-product, by
# resolve_category_and_type() returning (None, None), so crawling them is
# safe, just was never being attempted.
UMBRELLA_CATEGORY_URLS = {
    "https://www.marksandspencer.com.tr/kadin-giyim/",
}


def is_supported_category(name, url=None):
    """Delegates to ozon_mapping.is_known_product_type() -- MUST stay a
    single source of truth with resolve_category_and_type()'s own keyword
    groups. Confirmed live, 2026-09-04: this function used to hardcode its
    own, much shorter keyword list that silently drifted out of sync with
    resolve_category_and_type() every time a new product type was added
    there (boxer/trunk/brief/slip/hipster/pijama/t-shirt), so discovery
    kept skipping categories this pipeline could actually handle.

    `url` is optional (defaults to name-only checking) so existing callers
    don't break, but should be passed wherever available -- see
    UMBRELLA_CATEGORY_URLS above for why."""
    if url in UMBRELLA_CATEGORY_URLS:
        return True
    return is_known_product_type(name)


def load_priority_categories(path=PRIORITY_FILE):
    """Returns rows sorted by Proprity ascending, ties broken by original file order."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["_row_order"] = range(len(df))
    df = df.sort_values(["Proprity", "_row_order"], kind="stable")
    return df.to_dict("records")


def fetch_live_ms_identifiers():
    """Returns (article_codes_set, parent_skus_set) derived from every live MS-*
    offer_id on the Ozon account. offer_id shape is either:
      MS-{ms_article_code}-{color}-{size}   (this pipeline's own uploads)
      MS-{parent_sku}-{size}                (legacy/manual uploads, numeric SKU)
    We can't tell which shape a given offer_id is without re-deriving it, so we
    just record the raw token between "MS-" and the next "-" as a candidate for
    both sets — membership checks below use "is this article_code or parent_sku
    a substring-match token of some live offer_id" which works for either shape.
    """
    live_tokens = set()
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        for it in items:
            oid = it.get("offer_id", "")
            if oid.startswith("MS-"):
                live_tokens.add(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return live_tokens


def product_already_live(article_code, parent_sku, live_offer_ids):
    """True if this product (by either identifier) already appears in some live
    MS-* offer_id. Uses substring containment since offer_id embeds the
    identifier alongside color/size tokens we don't want to have to reconstruct."""
    for oid in live_offer_ids:
        if article_code and str(article_code) in oid:
            return True
        if parent_sku and str(parent_sku) in oid:
            return True
    return False


def estimate_offers(rows):
    """Rough count of the Ozon offers (one per in-stock size variant) a
    product would become: rows with stock_count > 0 / in_stock true, where
    stock is unknown for a row it counts (the upload decides later). Just an
    estimate -- dedup, color-risk skips and unmapped sizes drop some at upload,
    which is why discovery aims over the plan (DISCOVERY_OVERSHOOT_FACTOR)."""
    n = 0
    for r in rows or []:
        stock_count = r.get("stock_count")
        in_stock = r.get("in_stock")
        if stock_count is not None:
            try:
                if float(stock_count) > 0:
                    n += 1
            except (TypeError, ValueError):
                n += 1
        elif in_stock is not None:
            if in_stock:
                n += 1
        else:
            n += 1
    return n


def iter_new_products_in_category(category_url, live_offer_ids, already_queued):
    """Discovers product URLs in a category, fetches each one's article/parent
    SKU cheaply (via extract_product, same as a real crawl — there's no lighter
    listing-only signal available), and yields
    {"url", "name", "offers"} for each one not already live on Ozon and not
    already queued in product_urls.txt. Lazy, so the caller can stop fetching
    pages as soon as the day's target is reached."""
    from crawler import extract_product

    candidate_urls = discover_category_products(category_url) - already_queued
    for url in sorted(candidate_urls):
        try:
            rows = extract_product(url)
        except Exception as e:
            print(f"    ! could not check {url}: {e}")
            continue
        if not rows:
            continue
        article_code = rows[0].get("ms_article_code")
        parent_sku = rows[0].get("parent_sku")
        if not product_already_live(article_code, parent_sku, live_offer_ids):
            yield {"url": url, "name": rows[0].get("name"), "offers": estimate_offers(rows)}


def find_new_product_urls_in_category(category_url, live_offer_ids, already_queued):
    """List form of iter_new_products_in_category(): every new product's
    {"url", "name", "offers"} in the category."""
    return list(iter_new_products_in_category(category_url, live_offer_ids, already_queued))


def record_discovery_counts(found_by_cat, path=INVENTORY_FILE, today=None):
    """Merges today's per-(brand, category) "found on the M&S site, not live
    on Ozon" counts into category_inventory.json under "discovery_found",
    leaving every other key in the file untouched (category_inventory.py owns
    the rest of the file). Counts are what this walk saw before it stopped,
    so a partial walk gives a lower bound. Never raises -- a failed write
    must not fail discovery."""
    try:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        counts = {}
        for (brand, label), c in sorted(found_by_cat.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            counts.setdefault(brand, {})[label if label is not None else "Unmapped"] = {
                "products": c["products"], "offers": c["offers"]}
        data["discovery_found"] = {
            "date": today or daily_plan.utc_today(),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": counts,
        }
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"Could not record discovery counts to {path}: {e}")


def run_priority_cycle(target_new_products=None, per_category_target=None, fill_remainder=True):
    """Walks category_priority.csv from priority 1, queuing not-yet-live
    products into product_urls.txt for crawler.py, until the day's target is
    reached or every supported category has been checked.

    Business instruction, 2026-11-24: discovery aims for the DAILY PLAN
    (daily_plan.py, default 500 new offers/day), not the whole daily_create
    quota -- this replaces the 2026-09-01 "don't stop until the quota is
    full" rule. Targets are counted in estimated OFFERS (estimate_offers()),
    the plan's own unit, not URLs, and scaled by DISCOVERY_OVERSHOOT_FACTOR
    since upload-time dedup drops some; the upload itself is the hard cap
    and defers any overflow to deferred_items.json.

    target_new_products: total offers wanted (None = walk every category).
    per_category_target: {(brand, label): offers} allocations still open
      today (daily_plan.remaining_today()["per_category"]). None = no
      per-category limits. A category whose allocation is full -- or that
      has no allocation -- only gets more when fill_remainder is on and the
      total still has room.
    Returns a summary dict for logging."""
    categories = load_priority_categories()
    already_queued = load_line_set(URLS_FILE)

    total_cap = (math.ceil(target_new_products * DISCOVERY_OVERSHOOT_FACTOR)
                 if target_new_products is not None else None)
    cat_caps = None
    if per_category_target is not None:
        cat_caps = {k: math.ceil(v * DISCOVERY_OVERSHOOT_FACTOR) for k, v in per_category_target.items()}
    if total_cap is not None:
        print(f"Target: {target_new_products} offer(s) x {DISCOVERY_OVERSHOOT_FACTOR} = "
              f"{total_cap} estimated offer(s) to queue.")
    if cat_caps:
        print("Per-category targets (estimated offers): "
              + ", ".join(f"{b}/{l}={n}" for (b, l), n in cat_caps.items()))

    print("Fetching live MS-* offer_ids from Ozon to know what's already listed...")
    live_offer_ids = fetch_live_ms_identifiers()
    print(f"{len(live_offer_ids)} MS-* offer_id(s) currently live.\n")

    skipped_unsupported = 0
    categories_checked = 0
    total_new_found = 0
    total_offers_queued = 0
    skipped_full = 0
    skipped_unmapped = 0
    skipped_no_stock = 0
    categories_with_new = 0
    last_priority = None
    last_category_name = None
    queued_by_cat = {}
    found_by_cat = {}
    stopped_reason = "exhausted_all_categories"

    def total_full():
        return total_cap is not None and total_offers_queued >= total_cap

    def has_room(key):
        if cat_caps is None:
            return not total_full()
        if key in cat_caps and queued_by_cat.get(key, 0) < cat_caps[key]:
            return not total_full()
        return fill_remainder and not total_full()

    for row in categories:
        name = row.get("Category Name (TR)")
        url = row.get("Full URL")
        priority = row.get("Proprity")

        if not is_supported_category(name, url):
            skipped_unsupported += 1
            continue

        categories_checked += 1
        print(f"[priority {priority}] {name} — {url}")
        new_urls = []
        for item in iter_new_products_in_category(url, live_offer_ids, already_queued):
            label = daily_plan.category_label_for_name(item["name"])
            key = (BRAND, label)
            stats = found_by_cat.setdefault(key, {"products": 0, "offers": 0})
            stats["products"] += 1
            stats["offers"] += item["offers"]
            if label is None:
                skipped_unmapped += 1  # upload can't map it either -- skip rather than guess
                continue
            if item["offers"] <= 0:
                skipped_no_stock += 1  # nothing to create today; rediscovered once restocked
                continue
            if not has_room(key):
                skipped_full += 1
                continue
            new_urls.append(item["url"])
            already_queued.add(item["url"])
            queued_by_cat[key] = queued_by_cat.get(key, 0) + item["offers"]
            total_offers_queued += item["offers"]
            if total_full():
                break

        if new_urls:
            with open(URLS_FILE, "a", encoding="utf-8") as f:
                for u in sorted(new_urls):
                    f.write(u + "\n")
            total_new_found += len(new_urls)
            categories_with_new += 1
            last_priority = priority
            last_category_name = name
            print(f"  -> {len(new_urls)} new product(s) queued ({total_new_found} product(s), "
                  f"~{total_offers_queued} offer(s) total so far this run).\n")
        else:
            print("  -> nothing to queue here, moving to next category.\n")

        if total_full():
            print(f"Reached today's target of {total_cap} estimated offer(s) "
                  f"(plan-driven) -- stopping category walk.\n")
            stopped_reason = "plan_target_reached"
            break

    if stopped_reason == "exhausted_all_categories":
        print(f"Walked every supported category in the priority list -- {total_new_found} "
              f"new product(s) queued across {categories_with_new} categor(y/ies).\n")

    record_discovery_counts(found_by_cat)

    return {
        "stopped_at_priority": last_priority,
        "stopped_at_category": last_category_name,
        "new_urls_found": total_new_found,
        "estimated_offers_queued": total_offers_queued,
        "offer_target": total_cap,
        "queued_by_category": {f"{b}/{l}": n for (b, l), n in queued_by_cat.items()},
        "skipped_category_full": skipped_full,
        "skipped_unmapped": skipped_unmapped,
        "skipped_no_stock": skipped_no_stock,
        "categories_checked": categories_checked,
        "categories_skipped_unsupported": skipped_unsupported,
        "stopped_reason": stopped_reason,
    }


def quota_remaining_from(quota):
    """daily_create limit - usage from a /v4/product/info/limit response, or
    None if the response is missing."""
    if not quota:
        return None
    dc = quota.get("daily_create", {}) or {}
    return dc.get("limit", 0) - dc.get("usage", 0)


def plan_remaining_now(quota):
    """daily_plan.remaining_today() for right now, given a check_quota()
    response (None = quota unreadable; the plan cap still applies)."""
    plan = daily_plan.load_plan()
    created_total, created_by_cat = daily_plan.created_today()
    remaining = daily_plan.remaining_today(plan, quota_remaining_from(quota), created_total, created_by_cat)
    remaining["plan_total"] = daily_plan.plan_for_date(plan, None)["total"]
    remaining["created_total"] = created_total
    return remaining


def main():
    # Aim for the day's PLAN (daily_plan.py), capped by the remaining
    # daily_create quota -- business instruction, 2026-11-24. If the quota
    # can't be read the plan alone limits it.
    quota = None
    try:
        from upload_to_ozon import check_quota
        quota = check_quota()
    except Exception as e:
        print(f"Could not read quota ({e}) -- the daily plan alone sets the target.\n")

    remaining = plan_remaining_now(quota)
    target = remaining["total"]
    print(f"Daily plan: {remaining['created_total']}/{remaining['plan_total']} created today, "
          f"quota remaining {quota_remaining_from(quota)}, "
          f"{target} offer(s) left to aim for.\n")
    if target <= 0:
        summary = {"new_urls_found": 0, "estimated_offers_queued": 0,
                   "stopped_reason": "plan_already_met"}
        print(f"Daily plan ({remaining['plan_total']}) already met — nothing to discover.")
        print("Summary:", summary)
        return

    summary = run_priority_cycle(
        target_new_products=target,
        per_category_target=remaining["per_category"],
        fill_remainder=remaining["fill_remainder"],
    )
    print("Summary:", summary)


if __name__ == "__main__":
    main()
