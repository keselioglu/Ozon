"""
Cyclical, priority-ordered category discovery. Unlike crawler.py's
run_category_discovery() (which marks a category "done" forever once
processed), this walks category_priority.csv in priority order every day,
starting from priority 1, looking for a category that has at least one
product not yet live on Ozon. As soon as one is found, its new products are
queued for crawling and the walk stops for the day — the next run starts
back at priority 1.

"Already live on Ozon" is checked two ways, since the account has two upload
eras with different offer_id conventions:
  - legacy/manual uploads use the Turkish site's numeric SKU (parent_sku)
  - this pipeline's own uploads use MS-{ms_article_code}-{color}-{size}
A product only counts as "new" if neither its ms_article_code nor its
parent_sku appears in any live MS-* offer_id on the account.

Only category types the pipeline can actually map/translate are considered
(kulot/külot/tanga -> underwear, atlet -> tank top, matching
ozon_mapping.resolve_category_and_type). Any other category name in the
priority file is skipped automatically without being crawled — no crawl
requests are spent on categories nothing downstream can list.
"""
import sys

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from crawler import discover_category_products, load_line_set
from ozon_client import call

PRIORITY_FILE = "category_priority.csv"
URLS_FILE = "product_urls.txt"

_SUPPORTED_KEYWORDS = ("kulot", "külot", "tanga", "atlet")


def is_supported_category(name):
    lower = (name or "").lower()
    return any(kw in lower for kw in _SUPPORTED_KEYWORDS)


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


def find_new_product_urls_in_category(category_url, live_offer_ids, already_queued):
    """Discovers product URLs in a category, fetches each one's article/parent
    SKU cheaply (via extract_product, same as a real crawl — there's no lighter
    listing-only signal available), and returns just the URLs not already live
    on Ozon and not already queued in product_urls.txt."""
    from crawler import extract_product

    candidate_urls = discover_category_products(category_url) - already_queued
    if not candidate_urls:
        return []

    new_urls = []
    for url in candidate_urls:
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
            new_urls.append(url)

    return new_urls


def run_priority_cycle():
    """Walks category_priority.csv from priority 1, stopping at the first
    supported category with at least one not-yet-live product. Appends any
    found URLs to product_urls.txt for crawler.py's normal crawl pass to pick
    up. Returns a summary dict for logging."""
    categories = load_priority_categories()
    already_queued = load_line_set(URLS_FILE)

    print("Fetching live MS-* offer_ids from Ozon to know what's already listed...")
    live_offer_ids = fetch_live_ms_identifiers()
    print(f"{len(live_offer_ids)} MS-* offer_id(s) currently live.\n")

    skipped_unsupported = 0
    for row in categories:
        name = row.get("Category Name (TR)")
        url = row.get("Full URL")
        priority = row.get("Proprity")

        if not is_supported_category(name):
            skipped_unsupported += 1
            continue

        print(f"[priority {priority}] {name} — {url}")
        new_urls = find_new_product_urls_in_category(url, live_offer_ids, already_queued)

        if not new_urls:
            print("  -> nothing new here, moving to next category.\n")
            continue

        with open(URLS_FILE, "a", encoding="utf-8") as f:
            for u in sorted(new_urls):
                f.write(u + "\n")

        print(f"  -> {len(new_urls)} new product(s) found and queued. Stopping today's category walk.\n")
        return {
            "stopped_at_priority": priority,
            "stopped_at_category": name,
            "new_urls_found": len(new_urls),
            "categories_checked": categories.index(row) + 1 - skipped_unsupported,
            "categories_skipped_unsupported": skipped_unsupported,
        }

    print("Walked every supported category in the priority list — nothing new anywhere today.\n")
    return {
        "stopped_at_priority": None,
        "stopped_at_category": None,
        "new_urls_found": 0,
        "categories_checked": len(categories) - skipped_unsupported,
        "categories_skipped_unsupported": skipped_unsupported,
    }


def main():
    summary = run_priority_cycle()
    print("Summary:", summary)


if __name__ == "__main__":
    main()
