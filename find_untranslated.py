"""
Lists products in products.csv that have no entry in ozon_translations.py yet.
Used by the daily pipeline (human or agent) to know what needs translating
before running upload_to_ozon.py, since untranslated products are silently
skipped there rather than uploaded broken.
"""
import sys

import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_translations import PRODUCT_TRANSLATIONS


def main():
    try:
        df = pd.read_csv("products.csv", encoding="utf-8-sig")
    except FileNotFoundError:
        return print("products.csv not found. Run crawler.py first.")

    unique = df.drop_duplicates("ms_article_code")
    untranslated = unique[~unique["ms_article_code"].isin(PRODUCT_TRANSLATIONS.keys())]
    untranslated = untranslated[untranslated["ms_article_code"].notna()]

    if untranslated.empty:
        print("All crawled products have a translation on file.")
        return

    print(f"{len(untranslated)} product(s) need a translation entry in ozon_translations.py:\n")
    for _, row in untranslated.iterrows():
        print(f"{row['ms_article_code']} | {row['name']} | {row['specs_json']}")
        print(f"  url: {row['url']}\n")


if __name__ == "__main__":
    main()
