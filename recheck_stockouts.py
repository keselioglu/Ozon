"""
Re-checks M&S product pages behind sizes that were skipped from upload for
being out of stock, so a size that has since restocked gets picked up and
uploaded on a later daily_run.py run -- without spending crawl time/quota
re-visiting every already-crawled URL (business instruction, 2026-08-27,
GitHub issue #9: "When a size of a new product stock is 0, skip creating
this size... in the next days while checking the stocks if that size is in
stock then create it").

Problem this closes: crawler.py marks a URL as done in processed_log.txt
after its first crawl and never revisits it. upload_to_ozon.py already
skips any row with stock_count <= 0 (see build_ozon_item) and logs it to
upload_skipped.jsonl with reason "Zero/unknown stock" -- but since the URL
is never re-crawled, a restocked size stays permanently un-uploadable even
after M&S has it available again.

This script: reads upload_skipped.jsonl for zero-stock skips, looks up each
skipped SKU's product URL in products.csv (all size variants of one product
share a URL), re-fetches ONLY those distinct URLs (not the whole catalog),
and overwrites their rows in products.csv with fresh data -- reusing
crawler.py's own extract_product() and the same (url, variant_sku)
dedup-on-write logic, so a size that's now in stock will be picked up by
the next upload_to_ozon.py run exactly like a newly-crawled product would.

Meant to run as an extra daily_run.py step (after step 2 crawl, before step
3 translate/step 4 upload) -- see daily_run.py for wiring. Safe to run
standalone too.
"""
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json

import pandas as pd

from crawler import extract_product

SKIPPED_LOG = "upload_skipped.jsonl"
PRODUCTS_CSV = "products.csv"
ZERO_STOCK_REASON = "Zero/unknown stock"


def load_zero_stock_skus():
    skus = set()
    try:
        with open(SKIPPED_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if any(ZERO_STOCK_REASON in r for r in entry.get("reasons", [])):
                    sku = entry.get("sku")
                    if sku is not None:
                        skus.add(sku)
    except FileNotFoundError:
        pass
    return skus


def main():
    skus = load_zero_stock_skus()
    print(f"{len(skus)} SKU(s) previously skipped for zero stock.")

    if not skus:
        return print("Nothing to re-check.")

    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    matching_rows = df[df["variant_sku"].isin(skus)]
    urls_to_recheck = sorted(matching_rows["url"].dropna().unique())
    print(f"{len(urls_to_recheck)} distinct product page(s) to re-check.\n")

    if not urls_to_recheck:
        return print("No matching URLs found in products.csv (already re-checked, or SKUs no longer present).")

    rows = df.to_dict("records")
    restocked_count = 0

    for i, url in enumerate(urls_to_recheck, 1):
        print(f"[{i}/{len(urls_to_recheck)}] {url}")
        try:
            variant_rows = extract_product(url)
        except Exception as e:
            print(f"  ! Fetch error: {e}")
            time.sleep(1.2)
            continue

        if not variant_rows:
            print("  -> No product data found.")
            time.sleep(1.2)
            continue

        now_in_stock = [v for v in variant_rows if (v.get("stock_count") or 0) > 0
                         and v["variant_sku"] in skus]
        if now_in_stock:
            restocked_count += len(now_in_stock)
            for v in now_in_stock:
                print(f"  -> RESTOCKED: {v['size_label']} (sku={v['variant_sku']}, stock={v['stock_count']})")
        else:
            print("  -> still out of stock")

        rows.extend(variant_rows)
        time.sleep(1.2)  # same pacing as crawler.py, be polite to M&S

    pd.DataFrame(rows).drop_duplicates(subset=["url", "variant_sku"], keep="last").to_csv(
        PRODUCTS_CSV, index=False, encoding="utf-8-sig")

    print(f"\n{restocked_count} previously-out-of-stock size(s) now show real stock -- "
          f"{PRODUCTS_CSV} updated, will be picked up by the next upload_to_ozon.py run.")


if __name__ == "__main__":
    main()
