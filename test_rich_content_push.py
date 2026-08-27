"""
One-off live test: pushes attribute 11254 (rich content) for exactly ONE
offer_id, to check whether the community-corroborated schema (see
generate_rich_content.py's docstring) is actually accepted by Ozon before
trusting it for all 156 products (GitHub issue #7, "go test" from the
business, 2026-08-27).

Only touches ATTR_RICH_CONTENT -- every other attribute, price, and images
are carried through unchanged from the live record (same safe pattern as
push_hashtag_fixes.py).

Not part of daily_run.py -- a single manual test run.
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call

TEST_OFFER_ID = "MS-T81006849L-PINKMIX-40"
ATTR_RICH_CONTENT = 11254


def main():
    with open("generated_rich_content.jsonl", encoding="utf-8") as f:
        payloads = {json.loads(line)["article_code"]: json.loads(line)["rich_content"] for line in f}

    rich_content = payloads.get("T81006849L")
    if not rich_content:
        return print("No rich content payload found for T81006849L.")

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

    attributes = [a for a in record["attributes"] if a["id"] != ATTR_RICH_CONTENT]
    attributes.append({
        "id": ATTR_RICH_CONTENT,
        "values": [{"value": json.dumps(rich_content, ensure_ascii=False)}],
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

    print(f"Submitting test rich content for {TEST_OFFER_ID}...")
    result = call("/v3/product/import", {"items": [item]})
    task_id = result.get("result", {}).get("task_id")
    print(f"task_id={task_id}")
    print("Check status with: python -c \"from ozon_client import call; "
          f"print(call('/v1/product/import/info', {{'task_id': {task_id}}}))\"")


if __name__ == "__main__":
    main()
