"""
Ad-hoc live test (2026-08-27): pushes BOTH attribute 13164 (size chart,
issue #6) and attribute 11254 (rich content, issue #7) to a single product
that is confirmed live AND has real in-stock inventory -- the first test
product (MS-T81006849L-PINKMIX-34/-40) turned out to be out of stock, which
meant the business couldn't actually view it on the storefront to verify
rendering.

Test product: MAR-T81006470T-SIYAH-40EU (13 units in stock, confirmed live,
category 200001517/93238 -- same category as the rest of this pipeline).
This is a legacy MAR- listing not covered by PRODUCT_TRANSLATIONS or
products.csv (no on-file M&S source data), so this script builds minimal
but correct test content directly rather than skip it:
  - Size chart: women's underwear category table (kadin_ic_giyim), EU 40 /
    RU size via ozon_mapping, waist/hip from size_charts.py.
  - Rich content: uses the product's own EXISTING live Ozon-hosted image
    (no M&S CDN URL on file for this legacy product) with original wording
    built from its own live product name.

Not part of daily_run.py -- a single manual test run, superseding the
earlier out-of-stock test for verification purposes only (that test's
result already confirmed both schemas are accepted by Ozon; this one is
purely so the business can view a real, purchasable product).
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from ozon_mapping import map_size_to_ozon
from size_charts import SIZE_CHARTS

TEST_OFFER_ID = "MAR-T81006470T-SIYAH-40EU"
ATTR_SIZE_CHART = 13164
ATTR_RICH_CONTENT = 11254
EU_SIZE = "40"


def build_size_chart():
    chart = SIZE_CHARTS["kadin_ic_giyim"]
    eu_index = chart["columns"].index("Avrupa")
    row = next(r for r in chart["rows"] if r[eu_index] == EU_SIZE)
    _, ru_size, _ = map_size_to_ozon("40 (UK 12)")

    return {
        "content": [{
            "widgetName": "tcTable",
            "table": {
                "title": chart["title"],
                "body": [
                    {"data": [["RU", "Российский размер"], str(ru_size)]},
                    {"data": [["INT", "Международный размер"], EU_SIZE]},
                    {"data": [["От, см", "Объем талии, см"], row[chart["columns"].index("Bel (cm)")]]},
                    {"data": [["Об, см", "Объем бедер, см"], row[chart["columns"].index("Kalça (cm)")]]},
                ],
            },
        }],
        "version": 0.1,
    }


def build_rich_content(product_name, image_url):
    overview = (
        "Синтетический материал с кружевной отделкой. Продуманный крой обеспечивает "
        "комфортную посадку и мягкое прилегание к телу в течение всего дня."
    )
    return {
        "content": [
            {
                "widgetName": "raTextBlock",
                "text": {"size": "size2", "color": "color1", "content": [overview]},
            },
            {
                "widgetName": "raShowcase",
                "type": "chess",
                "blocks": [{
                    "img": {
                        "src": image_url, "srcMobile": image_url, "alt": product_name,
                        "width": 700, "height": 900, "widthMobile": 400, "heightMobile": 514,
                    },
                    "title": {"content": [product_name]},
                    "text": {"size": "size2", "align": "left", "color": "color1", "content": [overview]},
                }],
            },
            {
                "widgetName": "raTextBlock",
                "text": {
                    "size": "size2", "color": "color1",
                    "content": ["Рекомендации по уходу: ручная стирка при низкой температуре, не отбеливать."],
                },
            },
        ],
        "version": 0.3,
    }


def main():
    attrs_result = call("/v4/product/info/attributes", {"filter": {"offer_id": [TEST_OFFER_ID]}, "limit": 1})
    records = attrs_result.get("result", [])
    if not records:
        return print(f"{TEST_OFFER_ID} not found live.")
    record = records[0]

    prices_result = call("/v5/product/info/prices", {"filter": {"offer_id": [TEST_OFFER_ID]}, "limit": 1})
    price_info = prices_result["items"][0]["price"]

    size_chart = build_size_chart()
    rich_content = build_rich_content(record["name"], record["primary_image"])

    attributes = [a for a in record["attributes"] if a["id"] not in (ATTR_SIZE_CHART, ATTR_RICH_CONTENT)]
    attributes.append({"id": ATTR_SIZE_CHART, "values": [{"value": json.dumps(size_chart, ensure_ascii=False)}]})
    attributes.append({"id": ATTR_RICH_CONTENT, "values": [{"value": json.dumps(rich_content, ensure_ascii=False)}]})

    item = {
        "offer_id": record["offer_id"],
        "name": record["name"],
        "description_category_id": record["description_category_id"],
        "type_id": record["type_id"],
        "attributes": attributes,
        "price": str(price_info.get("price", "")),
        "currency_code": price_info.get("currency_code", "USD"),
        "vat": str(price_info.get("vat", "0")),
        "images": record.get("images", []),
        "primary_image": record.get("primary_image"),
        "weight": record.get("weight"),
        "weight_unit": record.get("weight_unit"),
        "depth": record.get("depth"),
        "width": record.get("width"),
        "height": record.get("height"),
        "dimension_unit": record.get("dimension_unit"),
    }

    print(f"Submitting test size-chart + rich-content for {TEST_OFFER_ID}...")
    result = call("/v3/product/import", {"items": [item]})
    task_id = result.get("result", {}).get("task_id")
    print(f"task_id={task_id}")
    print(f"product_id={record.get('id')}")
    print(f"sku={record.get('sku')}")


if __name__ == "__main__":
    main()
