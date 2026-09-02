"""
Enrolls eligible, in-stock, healthy-margin products into Ozon's promotional
campaigns (GitHub issue #10, business instruction 2026-08-27; margin rule
replaced 2026-09-02).

Margin rule (business instruction, 2026-09-02, superseding the original
55%-of-list-price floor): a candidate qualifies only if M&S's current cost
is <= 46% of the price it would be enrolled at (see margin_pricing.py).
The original 55%-of-Ozon-list-price rule is retired -- the 46%-of-M&S-cost
rule is the sole margin gate now (business decision, 2026-09-02: "retire
the 55% rule, use only 46% M&S-cost").

For Elastic Boosting (which offers a PRICE RANGE per candidate, not one
fixed price -- price_min_elastic/price_max_elastic): try enrolling at
price_max_elastic (the deepest discount) first; if that fails the 46% test,
retry at price_min_elastic (the shallowest discount, closer to list price);
skip entirely if both fail. This mirrors the reasoning validated manually
2026-09-02: ratio_vs_max is always >= ratio_vs_min (max_elastic is always
the lower price, so dividing the same M&S cost by it always gives an
equal-or-larger ratio) -- so checking max first and falling back to min is
the only useful order; checking min first and falling back to max can never
find anything max didn't already accept.

Stock: uses REAL stock (RFBS via /v4/product/info/stocks), not the stock
field returned by /v1/actions/candidates itself -- that field reads 0 for
this account's entire real inventory (confirmed live 2026-09-02: candidates
showing stock=0 turned out to have real stock as high as 100+ units). See
margin_pricing.get_real_stock.

Endpoints (community-corroborated against Go/Python Ozon API client
libraries, then verified live on this account, 2026-08-27):
  GET  /v1/actions                      -- list campaigns
  POST /v1/actions/candidates           -- list eligible-not-yet-enrolled products
  POST /v1/actions/products/activate    -- enroll products at a chosen price

Wired into daily_run.py as its own step (runs after refresh_prices.py, so
today's M&S prices are already loaded, and after refresh_live_stock.py).

Also writes campaign_eligibility_today.json every run (business
instruction, 2026-09-02: "lets daily check would auto-enroll today") -- one
row per campaign listing every candidate this script's OWN eligibility rule
(real stock + 46% M&S-cost margin) would enroll that day, resolved to
offer_id/name/price for readability. This is a report of what THIS script
enrolls, separate from Ozon's own algorithmic auto-add mechanism (Elastic
Boosting's auto_add_dates etc.) -- that mechanism has no API visibility at
all (confirmed 2026-09-02: Ozon selects and schedules its own candidates
for a future date, with no endpoint to read which specific products) and is
NOT what this report covers.
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

from margin_pricing import (
    compute_ratio_pct,
    fetch_usd_try_rate,
    get_real_stock,
    load_ms_prices,
    qualifies,
    resolve_offer_ids_and_names,
)
from ozon_client import call

load_dotenv()

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


def choose_enrollment(candidate, offer_id, real_stock, ms_prices, usd_try_rate):
    """Returns {"price": ..., "ratio_pct": ...} for the price this
    candidate should be enrolled at, or None if it should be skipped (no
    real stock, no resolvable M&S price, or every viable price exceeds the
    46% margin threshold).

    Elastic Boosting candidates carry price_min_elastic/price_max_elastic
    instead of a single max_action_price -- try the deeper (max_elastic)
    discount first, fall back to the shallower (min_elastic) one. Maximum
    Boosting / Summer Sale candidates only ever have max_action_price."""
    if real_stock <= 0:
        return None

    ms_entry = ms_prices.get(offer_id)
    if not ms_entry:
        return None
    ms_price_try, _url = ms_entry

    price_max_elastic = candidate.get("price_max_elastic")
    price_min_elastic = candidate.get("price_min_elastic")
    if price_max_elastic and price_min_elastic:
        for price in (price_max_elastic, price_min_elastic):
            ratio = compute_ratio_pct(ms_price_try, price, usd_try_rate)
            if qualifies(ratio):
                return {"price": price, "ratio_pct": round(ratio, 1)}
        return None

    max_action_price = candidate.get("max_action_price", 0)
    if not max_action_price:
        return None
    ratio = compute_ratio_pct(ms_price_try, max_action_price, usd_try_rate)
    if qualifies(ratio):
        return {"price": max_action_price, "ratio_pct": round(ratio, 1)}
    return None


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
    usd_try_rate = fetch_usd_try_rate()
    print(f"Live USD/TRY rate: {usd_try_rate}\n")

    ms_prices = load_ms_prices()
    print(f"{len(ms_prices)} offer_id(s) with a known M&S price today.\n")

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

        product_ids = [c["id"] for c in candidates]
        info_by_pid = resolve_offer_ids_and_names(product_ids)
        offer_ids = [info[0] for info in info_by_pid.values() if info[0]]
        real_stock_by_offer = get_real_stock(offer_ids)

        enrollments = []
        eligible_products = []
        skipped = 0
        for c in candidates:
            pid = c["id"]
            offer_id, name = info_by_pid.get(pid, (None, None))
            if not offer_id:
                skipped += 1
                continue
            real_stock = real_stock_by_offer.get(offer_id, 0)
            choice = choose_enrollment(c, offer_id, real_stock, ms_prices, usd_try_rate)
            if choice is None:
                skipped += 1
                continue
            enrollments.append((pid, choice["price"], real_stock))
            eligible_products.append({
                "product_id": pid, "offer_id": offer_id, "name": name,
                "enrollment_price": choice["price"], "stock": real_stock,
                "ms_ratio_pct": choice["ratio_pct"],
            })

        print(f"  {len(enrollments)} eligible for enrollment (real stock + M&S cost <= 46% of price), "
              f"{skipped} skipped (no stock / no M&S price / margin too thin).")

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
          f"{total_skipped} skipped (no stock / no M&S price / margin too thin), "
          f"{total_rejected} rejected by Ozon.")


if __name__ == "__main__":
    main()
