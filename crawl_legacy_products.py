"""
Crawls the M&S product pages behind legacy_product_urls.csv's URLs that
aren't already in products.csv, so the content-quality work already built
for this pipeline's own uploads (photos, video, rich content, size chart --
issues #4/#5/#6/#7/#8) can extend to the legacy catalog too (issue #11,
business instruction 2026-08-27: "Increase content quality of past M&S
products... whatever we do for new products").

Scope check (2026-08-28): legacy_product_urls.csv has 3,843 offer_id->URL
mappings collapsing to 510 distinct product URLs; only 16 of those were
already crawled into products.csv, leaving 494 pages to fetch. This script
appends their data to products.csv using the exact same extract_product()
crawler.py already uses for new products -- no separate data model, no
separate content-generation logic needed afterward. Once this runs, the
EXISTING scripts (generate_extra_photos.py, generate_product_videos.py,
generate_rich_content.py, generate_size_chart_table.py) already iterate
products.csv and will naturally pick up these legacy products too, with no
changes needed to any of them.

Rate-limited the same as crawler.py (1.2s between requests) to stay polite
to M&S. Safe to re-run: dedups on (url, variant_sku), so a partial/
interrupted run picks back up (skips URLs already crawled).
"""
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from crawler import extract_product

LEGACY_URL_MAP_FILE = "legacy_product_urls.csv"
PRODUCTS_CSV = "products.csv"
FAILED_LOG = "legacy_crawl_failed.txt"


def main():
    try:
        legacy_df = pd.read_csv(LEGACY_URL_MAP_FILE, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{LEGACY_URL_MAP_FILE} not found.")

    try:
        products_df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
        rows = products_df.to_dict("records")
        already_crawled_urls = set(products_df["url"].dropna())
    except (FileNotFoundError, pd.errors.EmptyDataError):
        rows = []
        already_crawled_urls = set()

    legacy_urls = sorted(legacy_df["URL"].dropna().unique())
    urls_to_crawl = [u for u in legacy_urls if u not in already_crawled_urls]

    print(f"{len(legacy_urls)} distinct legacy product URL(s), "
          f"{len(legacy_urls) - len(urls_to_crawl)} already crawled, "
          f"{len(urls_to_crawl)} left to crawl.\n")

    if not urls_to_crawl:
        return print("Nothing to do.")

    crawled_ok, failed = 0, 0
    for i, url in enumerate(urls_to_crawl, 1):
        print(f"[{i}/{len(urls_to_crawl)}] {url}")
        try:
            variant_rows = extract_product(url)
        except Exception as e:
            print(f"  ! Fetch error: {e}")
            with open(FAILED_LOG, "a", encoding="utf-8") as f:
                f.write(url + "\n")
            failed += 1
            time.sleep(1.2)
            continue

        if not variant_rows:
            print("  -> No product data found.")
            with open(FAILED_LOG, "a", encoding="utf-8") as f:
                f.write(url + "\n")
            failed += 1
            time.sleep(1.2)
            continue

        rows.extend(variant_rows)
        crawled_ok += 1
        print(f"  -> {variant_rows[0]['name']} | {len(variant_rows)} size variant(s)")

        # Write after every page, same as crawler.py, so an interrupted run
        # doesn't lose already-crawled progress.
        pd.DataFrame(rows).drop_duplicates(subset=["url", "variant_sku"], keep="last").to_csv(
            PRODUCTS_CSV, index=False, encoding="utf-8-sig")

        time.sleep(1.2)

    print(f"\n{crawled_ok} page(s) crawled successfully, {failed} failed (see {FAILED_LOG}).")
    print(f"{PRODUCTS_CSV} updated -- existing content-generation scripts "
          "(generate_extra_photos.py, generate_product_videos.py, generate_rich_content.py, "
          "generate_size_chart_table.py) will now cover these legacy products on their next run.")


if __name__ == "__main__":
    main()
