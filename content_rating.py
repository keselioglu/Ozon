"""
Read-only snapshot of Ozon's per-SKU content rating.

Lists every live, non-archived offer_id (default prefix 'MS-') via
/v3/product/list, resolves SKUs via /v3/product/info/list (response is FLAT:
`items` at the top level, not under `result` -- see CLAUDE.md), then reads
POST /v1/product/rating-by-sku in batches of up to 100 SKUs.

Outputs:
  content_rating_snapshot.json   -- overwritten each run, keyed by offer_id
  content_rating_history.jsonl   -- one summary line appended per run

READ-ONLY: this script never calls any endpoint that changes data.

Final line (parsed by daily_run.py):
  Done. N rated, avg X.X, M below 80.
"""
import argparse
import json
import sys
from collections import Counter
from datetime import date

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SNAPSHOT_FILE = "content_rating_snapshot.json"
HISTORY_FILE = "content_rating_history.jsonl"
RATING_BATCH = 100
INFO_BATCH = 1000
LOW_RATING_THRESHOLD = 80


# --- pure helpers (tested offline) -------------------------------------------

def extract_sku(item):
    """SKU from a /v3/product/info/list item: top-level `sku` if set, else
    the first non-zero `sources[].sku`. None if neither exists."""
    sku = item.get("sku")
    if sku:
        return int(sku)
    for src in item.get("sources") or []:
        if src.get("sku"):
            return int(src["sku"])
    return None


def _parse_group(group):
    unmet = [
        {"key": c.get("key"), "description": c.get("description"), "cost": c.get("cost")}
        for c in group.get("conditions") or []
        if not c.get("fulfilled")
    ]
    return {
        "key": group.get("key"),
        "name": group.get("name"),
        "rating": group.get("rating"),
        "weight": group.get("weight"),
        "improve_at_least": group.get("improve_at_least"),
        "unmet_conditions": unmet,
        "improve_attributes": [
            {"id": a.get("id"), "name": a.get("name")}
            for a in group.get("improve_attributes") or []
        ],
    }


def parse_rating_response(response, sku_to_offer):
    """Turn a /v1/product/rating-by-sku response into {offer_id: record}.

    sku_to_offer maps the SKUs that were requested to their offer_id. SKUs
    absent from the response are simply not in the result (callers can
    diff against sku_to_offer to see what's missing). SKUs in the response
    that weren't requested are ignored.
    """
    out = {}
    for product in (response or {}).get("products") or []:
        try:
            sku = int(product.get("sku"))
        except (TypeError, ValueError):
            continue
        offer_id = sku_to_offer.get(sku)
        if offer_id is None:
            continue
        out[offer_id] = {
            "offer_id": offer_id,
            "sku": sku,
            "rating": product.get("rating"),
            "groups": [_parse_group(g) for g in product.get("groups") or []],
        }
    return out


def summarize(snapshot, run_date=None):
    """History line for a snapshot ({offer_id: record}).

    Histogram buckets are 10-point wide, labelled by lower bound
    ("0", "10", ..., "90"); a rating of exactly 100 goes into "90".
    Records with no numeric rating count toward product_count but not
    toward the average/histogram.
    """
    ratings = []
    unmet = Counter()
    for rec in snapshot.values():
        r = rec.get("rating")
        if isinstance(r, (int, float)):
            ratings.append(float(r))
        for g in rec.get("groups") or []:
            for c in g.get("unmet_conditions") or []:
                if c.get("key"):
                    unmet[c["key"]] += 1
    histogram = {str(b): 0 for b in range(0, 100, 10)}
    for r in ratings:
        bucket = min(max(int(r // 10) * 10, 0), 90)
        histogram[str(bucket)] += 1
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "date": run_date or date.today().isoformat(),
        "product_count": len(snapshot),
        "rated_count": len(ratings),
        "average_rating": avg,
        "below_80": sum(1 for r in ratings if r < LOW_RATING_THRESHOLD),
        "histogram": histogram,
        "unmet_condition_counts": dict(sorted(unmet.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def format_done_line(summary):
    avg = summary["average_rating"]
    avg_s = f"{avg:.1f}" if avg is not None else "0.0"
    return f"Done. {summary['rated_count']} rated, avg {avg_s}, {summary['below_80']} below 80."


# --- network (read-only) -----------------------------------------------------

def fetch_live_products(prefix):
    """offer_id -> product_id for live, non-archived products with prefix."""
    from ozon_client import call
    products = {}
    cursor = ""
    while True:
        params = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        page = call("/v3/product/list", params).get("result", {})
        items = page.get("items", [])
        for item in items:
            oid = item.get("offer_id") or ""
            if oid.startswith(prefix) and not item.get("archived"):
                products[oid] = item["product_id"]
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return products


def fetch_skus(product_ids):
    """sku -> offer_id. /v3/product/info/list is flat (`items` top-level)."""
    from ozon_client import call
    sku_to_offer = {}
    product_ids = list(product_ids)
    for i in range(0, len(product_ids), INFO_BATCH):
        result = call("/v3/product/info/list", {"product_id": product_ids[i:i + INFO_BATCH]})
        for item in result.get("items", []):
            sku = extract_sku(item)
            if sku and item.get("offer_id"):
                sku_to_offer[sku] = item["offer_id"]
    return sku_to_offer


def fetch_ratings(sku_to_offer):
    from ozon_client import call
    snapshot = {}
    skus = list(sku_to_offer)
    for i in range(0, len(skus), RATING_BATCH):
        batch = skus[i:i + RATING_BATCH]
        try:
            resp = call("/v1/product/rating-by-sku", {"skus": batch})
        except RuntimeError as e:
            print(f"  rating batch {i // RATING_BATCH + 1} failed: {e}")
            continue
        snapshot.update(parse_rating_response(resp, {s: sku_to_offer[s] for s in batch}))
    return snapshot


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only Ozon content rating snapshot.")
    ap.add_argument("--limit", type=int, default=None, help="only rate the first N offer_ids (debug)")
    ap.add_argument("--offer-prefix", default="MS-", help="offer_id prefix to include (default 'MS-')")
    args = ap.parse_args(argv)

    products = fetch_live_products(args.offer_prefix)
    print(f"{len(products)} live offer_ids with prefix {args.offer_prefix!r}")
    if args.limit is not None:
        products = dict(sorted(products.items())[:args.limit])

    sku_to_offer = fetch_skus(products.values())
    print(f"{len(sku_to_offer)} SKUs resolved")

    snapshot = fetch_ratings(sku_to_offer)
    missing = len(sku_to_offer) - len(snapshot)
    if missing:
        print(f"{missing} SKUs missing from rating response")

    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(snapshot.items())), f, ensure_ascii=False, indent=1)
    summary = summarize(snapshot)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    print(format_done_line(summary))


if __name__ == "__main__":
    main()
