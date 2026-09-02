"""
Enrolls eligible, in-stock products into Ozon's promotional campaigns
(GitHub issue #10, business instruction 2026-08-27: "Frequently add
products to Ozon campaigns with prices min 55% of price when created").

Confirmed live (2026-08-27) via GET /v1/actions: this account currently
participates in 3 active campaigns (Эластичный бустинг, Максимальный
бустинг, Распродажа летнего). All three campaigns' "candidates" (products
eligible but not yet enrolled) showed 0 stock across the board when first
checked -- meaning real sellable inventory already appeared fully enrolled
at that point. This script is meant to run recurringly (wired into
daily_run.py) so any NEWLY uploaded/restocked product that becomes an
eligible candidate gets swept into its campaign(s) automatically going
forward, rather than needing another one-off backlog run.

Price rule (business + platform, both enforced): the campaign price must be
>= 55% of the product's regular price (business floor) AND <=
max_action_price (Ozon's own per-product ceiling for this campaign,
returned by /v1/actions/candidates -- confirmed live that Ozon's own
suggested elastic-pricing floor is already well above 55% in practice, so
the business's 55% rule is a safety floor that will rarely bind, not the
day-to-day operative constraint). A candidate whose max_action_price would
require going below the 55% floor is skipped rather than force-priced below
it.

Enrollment price chosen: max_action_price itself (the largest discount
Ozon will accept for this product in this campaign) UNLESS that violates
the 55% floor, in which case the candidate is skipped -- simplest rule that
satisfies "frequently add products... with prices min 55%" without
inventing an arbitrary intermediate discount depth.

Endpoints (community-corroborated against Go/Python Ozon API client
libraries, then verified live on this account, 2026-08-27):
  GET  /v1/actions                      -- list campaigns
  POST /v1/actions/candidates           -- list eligible-not-yet-enrolled products
  POST /v1/actions/products/activate    -- enroll products at a chosen price

Wired into daily_run.py as its own step (runs after live stock refresh).

Also writes campaign_eligibility_today.json every run (business
instruction, 2026-09-02: "lets daily check would auto-enroll today") -- one
row per campaign listing every candidate this script's OWN eligibility rule
(real stock + 55% price floor) would enroll that day, resolved to
offer_id/name/price for readability. This is a report of what THIS script
enrolls, separate from Ozon's own algorithmic auto-add mechanism (Elastic
Boosting's auto_add_dates etc.) -- that mechanism has no API visibility at
all (confirmed 2026-09-02: Ozon selects and schedules its own candidates
for a future date, e.g. "24 products, auto-adding 11 Sept 2026", with no
endpoint to read which specific products) and is NOT what this report
covers.
"""
import json
import sys
from datetime import date

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os

import requests
from dotenv import load_dotenv

from ozon_client import call

load_dotenv()

MIN_PRICE_FRACTION = 0.55  # business floor: action_price >= 0.55 * price
BATCH_SIZE = 100  # /v1/actions/products/activate's practical batch size
DAILY_REPORT_FILE = "campaign_eligibility_today.json"


def _get(method_path, params=None):
    """GET variant of ozon_client.call -- /v1/actions itself needs GET,
    unlike every POST-based endpoint elsewhere in this pipeline (confirmed
    live, 2026-08-27: POSTing to /v1/actions returns 405)."""
    headers = {
        "Client-Id": os.environ.get("OZON_CLIENT_ID"),
        "Api-Key": os.environ.get("OZON_API_KEY"),
    }
    resp = requests.get(f"https://api-seller.ozon.ru{method_path}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_active_campaigns():
    result = _get("/v1/actions")
    return result.get("result", [])


def list_candidates(action_id):
    """All eligible-but-not-yet-enrolled products for one campaign, paginated."""
    products = []
    offset = 0
    while True:
        result = call("/v1/actions/candidates", {"action_id": action_id, "limit": 1000, "offset": offset})
        batch = result.get("result", {}).get("products", [])
        if not batch:
            break
        products.extend(batch)
        offset += 1000
    return products


def choose_enrollment_price(candidate):
    """Returns the action_price to enroll at, or None if this candidate
    should be skipped (no real stock, or every viable price would violate
    the 55% floor)."""
    if candidate.get("stock", 0) <= 0:
        return None

    price = candidate.get("price", 0)
    max_action_price = candidate.get("max_action_price", 0)
    if not price or not max_action_price:
        return None

    floor = price * MIN_PRICE_FRACTION
    if max_action_price < floor:
        return None

    return max_action_price


def enroll_products(action_id, enrollments):
    """enrollments: [(product_id, action_price, stock), ...]. Returns
    (succeeded_ids, rejected)."""
    succeeded, rejected = [], []
    for i in range(0, len(enrollments), BATCH_SIZE):
        batch = enrollments[i:i + BATCH_SIZE]
        payload = {
            "action_id": action_id,
            "products": [
                {"product_id": pid, "action_price": price, "stock": stock}
                for pid, price, stock in batch
            ],
        }
        result = call("/v1/actions/products/activate", payload)
        succeeded.extend(result.get("result", {}).get("product_ids", []))
        rejected.extend(result.get("result", {}).get("rejected", []))
    return succeeded, rejected


def main():
    campaigns = list_active_campaigns()
    print(f"{len(campaigns)} campaign(s) available on this account.\n")

    total_enrolled, total_skipped, total_rejected = 0, 0, 0
    daily_report = {"date": date.today().isoformat(), "campaigns": []}

    for campaign in campaigns:
        action_id = campaign["id"]
        title = campaign.get("title", "")[:60]
        print(f"Campaign {action_id} - {title}")

        candidates = list_candidates(action_id)
        print(f"  {len(candidates)} candidate(s) not yet enrolled.")

        enrollments = []
        skipped = 0
        for c in candidates:
            enrollment_price = choose_enrollment_price(c)
            if enrollment_price is None:
                skipped += 1
                continue
            enrollments.append((c["id"], enrollment_price, c.get("stock", 0)))

        print(f"  {len(enrollments)} eligible for enrollment (real stock + price >= 55% floor), "
              f"{skipped} skipped (no stock or price floor violated).")

        # Resolve names for anything eligible TODAY, for the daily report --
        # business instruction (2026-09-02): "lets daily check would
        # auto-enroll today". This is a read-only report of what THIS
        # script would enroll on its own eligibility rule (real stock +
        # 55% price floor) -- separate from Ozon's own algorithmic
        # auto_add_dates mechanism (Elastic Boosting etc.), which has no
        # API visibility at all (confirmed 2026-09-02) and is not what this
        # reports.
        eligible_products = []
        if enrollments:
            product_ids = [pid for pid, _, _ in enrollments]
            names_by_id = {}
            for i in range(0, len(product_ids), 1000):
                batch = product_ids[i:i + 1000]
                info = call("/v3/product/info/list", {"product_id": batch})
                for item in info.get("result", {}).get("items", []):
                    names_by_id[item["id"]] = (item.get("offer_id"), item.get("name"))
            for pid, price, stock in enrollments:
                offer_id, name = names_by_id.get(pid, (None, None))
                eligible_products.append({
                    "product_id": pid, "offer_id": offer_id, "name": name,
                    "enrollment_price": price, "stock": stock,
                })

        if enrollments:
            succeeded, rejected = enroll_products(action_id, enrollments)
            print(f"  -> {len(succeeded)} enrolled, {len(rejected)} rejected by Ozon.")
            for r in rejected[:10]:
                print(f"     rejected product_id={r.get('product_id')}: {r.get('reason')}")
            total_enrolled += len(succeeded)
            total_rejected += len(rejected)

        total_skipped += skipped
        daily_report["campaigns"].append({
            "action_id": action_id,
            "title": campaign.get("title", ""),
            "candidate_count": len(candidates),
            "eligible_today": eligible_products,
        })
        print()

    with open(DAILY_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(daily_report, f, ensure_ascii=False, indent=2)
    total_eligible = sum(len(c["eligible_today"]) for c in daily_report["campaigns"])
    print(f"Daily eligibility report ({total_eligible} product(s) eligible today across all campaigns) "
          f"saved to {DAILY_REPORT_FILE}.\n")

    print(f"Done. {total_enrolled} product(s) newly enrolled across {len(campaigns)} campaign(s), "
          f"{total_skipped} skipped (no stock / price floor), {total_rejected} rejected by Ozon.")


if __name__ == "__main__":
    main()
