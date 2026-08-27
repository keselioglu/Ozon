"""
Builds Ozon's "Таблица размеров JSON" (attribute 13164) per offer_id, using
the real, confirmed schema exported from the Ozon Seller portal's own visual
size-table constructor (business-supplied example, 2026-08-27, GitHub issue
#6) -- NOT a guessed/community schema like the rich-content one:

    {
      "content": [
        {
          "widgetName": "tcTable",
          "table": {
            "title": "...",
            "body": [
              {"data": [["RU", "Российский размер"], "42"]},
              {"data": [["INT", "Международный размер"], "36"]},
              {"data": [["Ог, см", "Обхват груди, см"], ""]},
              ...
            ]
          }
        }
      ],
      "version": 0.1
    }

Each row is ONE measurement dimension for THIS SPECIFIC garment size (not a
whole size-range table like the size-chart IMAGE in size_charts.py /
generate_size_chart_images.py, issue #5) -- confirmed by the business
example, which encodes a single RU 42 / INT 36 row. So unlike the image
(one per category), the JSON table is one per (offer_id, its own size) --
matching how Ozon actually presents it: a buyer viewing THIS listing sees
just its own size's measurements, not a full chart.

Measurement values (chest/waist/hip in cm) come from size_charts.py's
category tables, matched by this offer_id's own EU size within its
category's row list. Left as "" when the category's chart doesn't have that
particular measurement (e.g. socks have no chest/waist), matching the
business example's own use of "" for unknown/inapplicable values.

Not part of daily_run.py -- a one-time content build per product. Pushing
live is a separate follow-up (this only builds and saves the JSON locally).
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from ozon_mapping import map_size_to_eu, map_size_to_ozon
from size_charts import SIZE_CHARTS, classify_category

PRODUCTS_CSV = "products.csv"
OUTPUT_FILE = "generated_size_chart_tables.jsonl"
SIZE_CHART_VERSION = 0.1

# Maps a category's measurement columns (beyond Beden/İngiltere/Avrupa) to
# the (short_label, long_label) pairs used in the business's own example --
# Ог/Обхват груди (chest), От/Объем талии (waist), Об/Объем бедер (hip).
# Column-name -> Russian label pairs, matched against size_charts.py's
# "columns" lists so this stays correct if a category's columns change.
MEASUREMENT_LABELS = {
    "Göğüs (cm)": ("Ог, см", "Обхват груди, см"),
    "Bel (cm)": ("От, см", "Объем талии, см"),
    "Kalça (cm)": ("Об, см", "Объем бедер, см"),
    "Basen (cm)": ("Об, см", "Объем бедер, см"),
}


def find_row_for_eu_size(category_key, eu_size):
    """category_key's row whose Avrupa (EU) column matches eu_size exactly,
    or None if this category has no Avrupa column or no matching row (e.g.
    the bra cup-conversion table has no EU size column at all)."""
    chart = SIZE_CHARTS.get(category_key)
    if not chart or "Avrupa" not in chart["columns"]:
        return None
    eu_index = chart["columns"].index("Avrupa")
    for row in chart["rows"]:
        if row[eu_index] == str(eu_size):
            return chart, row
    return None


def build_size_table_json(category_key, eu_size, ru_size):
    result = find_row_for_eu_size(category_key, eu_size)
    body = [
        {"data": [["RU", "Российский размер"], str(ru_size) if ru_size else ""]},
        {"data": [["INT", "Международный размер"], str(eu_size) if eu_size else ""]},
    ]

    if result:
        chart, row = result
        for col_name, label_pair in MEASUREMENT_LABELS.items():
            if col_name in chart["columns"]:
                idx = chart["columns"].index(col_name)
                body.append({"data": [list(label_pair), row[idx]]})
    else:
        # Still include the three standard measurement rows (matching the
        # business's own example shape) even when we have no data for them,
        # using "" exactly as their example did for values not applicable.
        for label_pair in MEASUREMENT_LABELS.values():
            if label_pair not in [tuple(b["data"][0]) for b in body]:
                body.append({"data": [list(label_pair), ""]})

    chart_title = SIZE_CHARTS.get(category_key, {}).get("title", "Таблица размеров")
    return {
        "content": [
            {
                "widgetName": "tcTable",
                "table": {"title": chart_title, "body": body},
            }
        ],
        "version": SIZE_CHART_VERSION,
    }


def main():
    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    from upload_to_ozon import build_sku

    built, skipped = 0, 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            article_code = row.get("ms_article_code")
            if pd.isna(article_code) or not article_code:
                continue

            category_key = classify_category(row.get("url"))
            eu_size, _ = map_size_to_eu(row.get("size_label"))
            _, ru_size, _ = map_size_to_ozon(row.get("size_label"))

            if not eu_size and not ru_size:
                skipped += 1
                continue

            offer_id = build_sku(article_code, row.get("color"), eu_size or "")
            size_table = build_size_table_json(category_key, eu_size, ru_size)

            f.write(json.dumps({
                "offer_id": offer_id,
                "size_table": size_table,
            }, ensure_ascii=False) + "\n")
            built += 1

    print(f"{built} size-chart table(s) built, {skipped} skipped (no resolvable size), "
          f"saved to {OUTPUT_FILE}.")
    print("NOT pushed to Ozon yet -- recommend testing one payload against a single live "
          "offer_id via /v3/product/import first, since this is the confirmed schema but "
          "our own category-matching logic hasn't been tested live yet.")


if __name__ == "__main__":
    main()
