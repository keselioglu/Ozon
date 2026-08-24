import json
import re
import sys
import time
import random

import cloudscraper
import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

URLS_FILE = "product_urls.txt"
CATEGORY_URLS_FILE = "category_urls.txt"
CATEGORY_LOG_FILE = "category_processed_log.txt"
OUTPUT_FILE = "products.csv"
LOG_FILE = "processed_log.txt"
FAILED_FILE = "failed_urls.txt"

# Product detail pages end in a long numeric M&S SKU, e.g. ".../kadin-...-10000001379295/"
PRODUCT_LINK_RE = re.compile(r'href="(/[^"]*\d{9,}[^"]*/)"')

JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)

# Size variants are rendered as custom <pz-variant-option> elements inside the
# key="integration_size" block, one per size, each carrying its own SKU/stock/label.
SIZE_BLOCK_RE = re.compile(
    r'<pz-variant\s+(?:class="[^"]*"\s+)?key="integration_size".*?</pz-variant>', re.DOTALL
)
SIZE_OPTION_RE = re.compile(r'<pz-variant-option\b(.*?)>(.*?)</pz-variant-option>', re.DOTALL)
ATTR_RE = re.compile(r'''(\w[\w-]*)=["']([^"']*)["']''')

# The M&S global article code (e.g. "T81006849L") — shared across all color/size
# variants of the same product — lives in the size-guide widget's data attribute.
# This is what the business uses as "the product code", distinct from the Turkish
# site's own numeric SKU (e.g. "10000001457042").
MS_ARTICLE_CODE_RE = re.compile(r'data-unique-product-id="([^"]+)"')

scraper = cloudscraper.create_scraper()


def find_product_jsonld(html):
    """The page can carry multiple JSON-LD blocks (breadcrumbs, website, product).
    Pick the one with @type == Product."""
    for block in JSONLD_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        elif isinstance(data, dict) and data.get("@type") == "Product":
            return data
    return None


def extract_size_variants(html):
    """Returns a list of {sku, size_value, size_label, stock, in_stock} per size option."""
    block_match = SIZE_BLOCK_RE.search(html)
    if not block_match:
        return []

    variants = []
    for attrs_str, _inner in SIZE_OPTION_RE.findall(block_match.group(0)):
        attrs = dict(ATTR_RE.findall(attrs_str))
        if not attrs.get("data-sku"):
            continue
        stock = attrs.get("data-stock")
        variants.append({
            "variant_sku": attrs.get("data-sku"),
            "size_value": attrs.get("value"),
            "size_label": attrs.get("label"),
            "stock_count": int(stock) if stock and stock.isdigit() else None,
            "in_stock": bool(stock) and stock != "0",
        })
    return variants


def extract_product(url):
    response = scraper.get(url, timeout=20)
    response.raise_for_status()
    html = response.text

    product = find_product_jsonld(html)
    if not product:
        return None

    offer = product.get("offers", {})
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    price_spec = offer.get("priceSpecification", {})

    rating = product.get("aggregateRating", {})

    specs = {}
    for prop in product.get("additionalProperty", []) or []:
        name = prop.get("name")
        value = prop.get("value", prop.get("unitText"))
        if name:
            specs[name] = value

    images = product.get("image", [])
    if isinstance(images, str):
        images = [images]

    article_match = MS_ARTICLE_CODE_RE.search(html)

    base_fields = {
        "url": url,
        "parent_sku": product.get("sku"),
        "ms_article_code": article_match.group(1) if article_match else None,
        "gtin": product.get("gtin"),
        "name": product.get("name"),
        "brand": (product.get("brand") or {}).get("name"),
        "description": product.get("description"),
        "color": specs.get("Renk"),
        "price": price_spec.get("price") or offer.get("price"),
        "currency": price_spec.get("priceCurrency") or offer.get("priceCurrency"),
        "availability": offer.get("availability"),
        "rating_value": rating.get("ratingValue"),
        "review_count": rating.get("reviewCount"),
        "image_urls": " | ".join(images),
        "specs_json": json.dumps(specs, ensure_ascii=False),
    }

    size_variants = extract_size_variants(html)
    if not size_variants:
        # No size-variant block found — still emit one row so the product isn't silently dropped.
        return [{**base_fields, "variant_sku": base_fields["parent_sku"], "size_value": None,
                  "size_label": None, "stock_count": None, "in_stock": None}]

    return [{**base_fields, **variant} for variant in size_variants]


def load_line_set(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def discover_category_products(category_url, max_pages=50):
    """Walks a category listing's ?page=N pagination and returns the set of
    absolute product detail page URLs found. Stops when a page yields nothing new."""
    from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode

    base = category_url.rstrip("/")
    split = urlsplit(category_url)
    query = parse_qs(split.query)

    found = set()
    for page in range(1, max_pages + 1):
        query["page"] = [str(page)]
        page_url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query, doseq=True), ""))
        try:
            resp = scraper.get(page_url, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"    ! page {page} fetch error: {e}")
            break

        links = PRODUCT_LINK_RE.findall(resp.text)
        page_products = {urljoin("https://www.marksandspencer.com.tr/", link) for link in links}
        new_products = page_products - found

        print(f"    page {page}: {len(page_products)} product link(s), {len(new_products)} new")
        if not new_products and page > 1:
            break
        found |= new_products
        time.sleep(random.uniform(1.0, 2.0))

    return found


def run_category_discovery():
    all_categories = list(load_line_set(CATEGORY_URLS_FILE))
    if not all_categories:
        return

    processed_categories = load_line_set(CATEGORY_LOG_FILE)
    new_categories = [c for c in all_categories if c not in processed_categories]
    if not new_categories:
        return

    existing_product_urls = load_line_set(URLS_FILE)
    all_found = set()

    print(f"--- Category discovery: {len(new_categories)} new categor(y/ies) ---")
    for cat_url in new_categories:
        print(f"  {cat_url}")
        found = discover_category_products(cat_url)
        print(f"  -> {len(found)} product(s) found\n")
        all_found |= found
        with open(CATEGORY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(cat_url + "\n")

    new_urls = all_found - existing_product_urls
    if new_urls:
        with open(URLS_FILE, "a", encoding="utf-8") as f:
            for url in sorted(new_urls):
                f.write(url + "\n")
        print(f"Added {len(new_urls)} new product URL(s) to {URLS_FILE}.\n")
    else:
        print("No new product URLs beyond what's already queued.\n")


def main():
    run_category_discovery()

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        all_urls = [line.strip() for line in f if line.strip()]

    if not all_urls:
        return print(f"{URLS_FILE} is empty. Add one product URL per line, or a category URL to {CATEGORY_URLS_FILE}.")

    processed = load_line_set(LOG_FILE)
    urls_to_process = [u for u in all_urls if u not in processed]

    try:
        rows = pd.read_csv(OUTPUT_FILE, encoding="utf-8-sig").to_dict("records")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        rows = []

    print(f"{len(all_urls)} total URLs | {len(processed)} already processed | {len(urls_to_process)} left.\n")

    if not urls_to_process:
        print("Nothing to do. Add more URLs to product_urls.txt to continue.")
        return

    try:
        for i, url in enumerate(urls_to_process, 1):
            print(f"[{i}/{len(urls_to_process)}] {url}")
            try:
                variant_rows = extract_product(url)
            except Exception as e:
                print(f"  ! Fetch error: {e}")
                variant_rows = None

            with open(LOG_FILE, "a", encoding="utf-8") as lf:
                lf.write(url + "\n")

            if variant_rows:
                rows.extend(variant_rows)
                name = variant_rows[0]["name"]
                print(f"  -> {name} | {len(variant_rows)} size variant(s)")
                for v in variant_rows:
                    stock_note = f"stock={v['stock_count']}" if v["size_value"] else ""
                    print(f"       {v['size_label'] or '(no size)'} | sku={v['variant_sku']} {stock_note}")
            else:
                print("  -> No product data found (no matching JSON-LD block).")
                with open(FAILED_FILE, "a", encoding="utf-8") as ff:
                    ff.write(url + "\n")

            if rows:
                pd.DataFrame(rows).drop_duplicates(subset=["url", "variant_sku"], keep="last").to_csv(
                    OUTPUT_FILE, index=False, encoding="utf-8-sig"
                )

            time.sleep(random.uniform(1.0, 2.0))

    except KeyboardInterrupt:
        print("\nPaused. Progress saved — re-run to resume.")
        sys.exit(0)

    print("\nDone.")


if __name__ == "__main__":
    main()
