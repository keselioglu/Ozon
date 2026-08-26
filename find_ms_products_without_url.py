"""
Reports every live M&S-family product (any prefix: MS-, MAR-, SML-, MARKS-,
MARK-, SMLMS-) that has no known source URL — meaning refresh_live_stock.py
can never verify or refresh its stock, and it just sits with whatever stock
value it was last given, indefinitely.

Distinct from "H&M" or other brands on the account (OYS-, LEVIS-, etc. were
already archived; H&M is a separate live brand, intentionally excluded here).

Groups results by underlying product (M&S article code, or the base numeric
SKU for legacy offer_ids that don't have one), since most size/color variants
of the same product need only one representative URL to unblock them all.

Output: ms_products_no_url.txt (gitignored, local report — re-run any time
after adding entries to legacy_product_urls.csv to see the remaining gap).
"""
import json
import re
import sys
from collections import defaultdict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from ozon_mapping import ARTICLE_CODE_IN_OFFER_ID_RE
from refresh_live_stock import load_legacy_url_map, load_pipeline_url_map
from category_priority import fetch_live_ms_identifiers

MS_FAMILY_PREFIXES = ("MS-", "MAR-", "SML-", "MARKS-", "MARK-", "SMLMS-")
REPORT_FILE = "ms_products_no_url.txt"


def find_live_ms_family_offer_ids():
    """Every live (non-archived) offer_id under any M&S naming convention."""
    matches = set()
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        for item in items:
            oid = item.get("offer_id", "")
            if oid.startswith(MS_FAMILY_PREFIXES) and not item.get("archived"):
                matches.add(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return matches


def group_by_product(offer_ids):
    """Groups offer_ids by their underlying product — M&S article code when
    present, else the base numeric SKU (legacy offer_ids without an article
    code embed the Turkish-site numeric SKU instead)."""
    groups = defaultdict(list)
    for oid in offer_ids:
        m = ARTICLE_CODE_IN_OFFER_ID_RE.search(oid)
        if m:
            key = m.group(0)
        else:
            key = next((p for p in oid.split("-") if p.isdigit() and len(p) >= 6), oid)
        groups[key].append(oid)
    return groups


def main():
    print("Finding all live M&S-family offer_ids (MS-, MAR-, SML-, MARKS-, MARK-, SMLMS-)...")
    ms_family = find_live_ms_family_offer_ids()
    print(f"{len(ms_family)} live M&S-family offer_id(s) total.\n")

    live_ms = fetch_live_ms_identifiers()
    legacy_map = load_legacy_url_map()
    pipeline_map = load_pipeline_url_map(live_ms)
    known = set(legacy_map) | set(pipeline_map)

    no_url = ms_family - known
    print(f"{len(no_url)} have NO known source URL.\n")

    groups = group_by_product(no_url)
    print(f"That's {len(groups)} distinct product(s) (grouped by article code / SKU).\n")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(f"{len(no_url)} live M&S offer_ids across {len(groups)} distinct products "
                 "have no known source URL.\n")
        f.write("Grouped by article code / SKU — one representative offer_id per group is "
                 "enough to find the M&S page and unblock every variant.\n\n")
        for key in sorted(groups.keys()):
            oids = sorted(groups[key])
            f.write(f"{key}  ({len(oids)} variant(s))\n")
            for oid in oids:
                f.write(f"  {oid}\n")
            f.write("\n")

    print(f"Full report written to {REPORT_FILE}.")


if __name__ == "__main__":
    main()
