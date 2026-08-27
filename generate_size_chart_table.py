"""
Builds Ozon's "Таблица размеров JSON" (attribute 13164) per CATEGORY, using
the real schema confirmed from a live competitor listing's own Network-tab
API response (business-supplied, 2026-08-27, GitHub issue #6):

    {
      "content": [
        {
          "widgetName": "tcTable",
          "table": {
            "title": "...",  <- must be RUSSIAN (first test used a Turkish
                                 title, business flagged this as wrong)
            "body": [
              {"data": [["RU", "Российский размер"], "40", "42", "44", ...]},
              {"data": [["INT", "Международный размер"], "34", "36", "38", ...]},
              {"data": [["От, см", "Объем талии, см"], "61", "65", "70", ...]},
              {"data": [["Об, см", "Объем бедер, см"], "86", "90", "95", ...]},
              ...
            ]
          }
        }
      ],
      "version": 0.1
    }

Each row's `data` is `[[short_label, long_label], value_for_col_1,
value_for_col_2, ...]` -- one column per SIZE, so the whole chart's size
range appears on every product in that category (business explicitly
wanted "all sizes... seen in each product", not just the product's own
size -- confirmed 2026-08-27 after the business viewed the first,
single-size-only test and asked for this).

So THE SAME table (one per category) is pushed to every offer_id in that
category -- it doesn't vary per product the way the per-product rich
content does. size_charts.py's category tables are transposed here: each
of its rows (one per size) becomes a column in the Ozon table; each of its
measurement columns (Bel/Kalça/Göğüs/etc.) becomes one row.

Not part of daily_run.py -- a one-time content build. Pushing live is a
separate follow-up (this only builds and saves the JSON locally, then
associates it with every relevant offer_id).
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from size_charts import SIZE_CHARTS, classify_category

PRODUCTS_CSV = "products.csv"
OUTPUT_FILE = "generated_size_chart_tables.jsonl"
SIZE_CHART_VERSION = 0.1

# Maps a category's measurement columns to the (short_label, long_label)
# pairs Ozon expects (Ог/Обхват груди = chest, От/Объем талии = waist,
# Об/Объем бедер = hip) -- matched against size_charts.py's own "columns"
# names so this stays correct if a category's columns ever change.
MEASUREMENT_LABELS = {
    "Göğüs (cm)": ("Ог, см", "Обхват груди, см"),
    "Bel (cm)": ("От, см", "Объем талии, см"),
    "Kalça (cm)": ("Об, см", "Объем бедер, см"),
    "Basen (cm)": ("Об, см", "Объем бедер, см"),
}


def build_category_size_table(category_key):
    """One tcTable JSON for the whole category, with one column per size
    (per size_charts.py's row list) and one row per measurement dimension
    -- the transpose of how size_charts.py stores it (one row per size)."""
    chart = SIZE_CHARTS[category_key]
    columns = chart["columns"]
    rows = chart["rows"]

    body = []

    if "Beden" in columns:
        beden_idx = columns.index("Beden")
        body.append({"data": [["Размер", "Буквенный размер"], *[r[beden_idx] for r in rows]]})

    if "İngiltere" in columns:
        uk_idx = columns.index("İngiltere")
        body.append({"data": [["UK", "Британский размер"], *[r[uk_idx] for r in rows]]})

    if "Avrupa" in columns:
        eu_idx = columns.index("Avrupa")
        body.append({"data": [["INT", "Международный размер"], *[r[eu_idx] for r in rows]]})

    for col_name, label_pair in MEASUREMENT_LABELS.items():
        if col_name in columns:
            idx = columns.index(col_name)
            body.append({"data": [list(label_pair), *[r[idx] for r in rows]]})

    return {
        "content": [{
            "widgetName": "tcTable",
            "table": {"title": chart.get("title_ru", chart["title"]), "body": body},
        }],
        "version": SIZE_CHART_VERSION,
    }


def main():
    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    from upload_to_ozon import build_sku
    from ozon_mapping import map_size_to_eu

    # Build each category's table once (it's identical for every product in
    # that category -- all sizes always shown), then reuse it.
    category_tables = {key: build_category_size_table(key) for key in SIZE_CHARTS}

    built, skipped = 0, 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            article_code = row.get("ms_article_code")
            if pd.isna(article_code) or not article_code:
                continue

            category_key = classify_category(row.get("url"))
            if not category_key:
                skipped += 1
                continue

            eu_size, _ = map_size_to_eu(row.get("size_label"))
            if not eu_size:
                skipped += 1
                continue

            offer_id = build_sku(article_code, row.get("color"), eu_size)
            f.write(json.dumps({
                "offer_id": offer_id,
                "size_table": category_tables[category_key],
            }, ensure_ascii=False) + "\n")
            built += 1

    print(f"{built} size-chart table(s) built ({len(category_tables)} distinct category table(s), "
          f"reused across matching products), {skipped} skipped, saved to {OUTPUT_FILE}.")
    print("NOT pushed to Ozon yet -- recommend testing one payload against a single live "
          "offer_id via /v3/product/import first to confirm it renders correctly (all sizes "
          "visible, Russian title).")


if __name__ == "__main__":
    main()
