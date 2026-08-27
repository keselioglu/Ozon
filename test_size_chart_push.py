"""
One-off live test: pushes attribute 13164 (size chart table) for exactly
ONE offer_id, to confirm the business-supplied schema (see
generate_size_chart_table.py) actually renders correctly on the live PDP
before pushing all 2,426 payloads (GitHub issue #6, 2026-08-27).

Only touches ATTR_SIZE_CHART -- every other attribute, price, and images are
carried through unchanged from the live record (same safe pattern as
push_hashtag_fixes.py / test_rich_content_push.py).

Not part of daily_run.py -- a single manual test run.
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call

TEST_OFFER_ID = "MS-T81006849L-PINKMIX-34"
ATTR_SIZE_CHART = 13164


def main():
    with open("generated_size_chart_tables.jsonl", encoding="utf-8") as f:
        payloads = {json.loads(line)["offer_id"]: json.loads(line)["size_table"] for line in f}

    size_table = payloads.get(TEST_OFFER_ID)
    if not size_table:
        return print(f"No size-chart payload found for {TEST_OFFER_ID}.")

    attrs_result = call("/v4/product/info/attributes", {"filter": {"offer_id": [TEST_OFFER_ID]}, "limit": 1})
    records = attrs_result.get("result", [])
    if not records:
        return print(f"{TEST_OFFER_ID} not found live.")
    record = records[0]

    prices_result = call("/v5/product/info/prices", {"filter": {"offer_id": [TEST_OFFER_ID]}, "limit": 1})
    price_items = prices_result.get("items", [])
    if not price_items:
        return print(f"No price found for {TEST_OFFER_ID}.")
    price_info = price_items[0]["price"]

    attributes = [a for a in record["attributes"] if a["id"] != ATTR_SIZE_CHART]
    attributes.append({
        "id": ATTR_SIZE_CHART,
        "values": [{"value": json.dumps(size_table, ensure_ascii=False)}],
    })

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

    print(f"Submitting test size-chart table for {TEST_OFFER_ID}...")
    result = call("/v3/product/import", {"items": [item]})
    task_id = result.get("result", {}).get("task_id")
    print(f"task_id={task_id}")
    print(f"product_id={record.get('id')}")


if __name__ == "__main__":
    main()
