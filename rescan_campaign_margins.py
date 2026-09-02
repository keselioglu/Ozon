"""
Daily post-enrollment margin re-scan (business instruction, 2026-09-02:
after finding 288 Elastic Boosting products whose live price had drifted
past the 46% margin threshold via Ozon's own current_boost mechanism --
"check again how many candidates remaining" / "remove all these 288 from
campaigns" / "check if this 288 products can be added to any campaign that
will be under 46% product cost").

Root cause this step guards against: Elastic Boosting's current_boost is
continuously adjusted by Ozon within a product's min_boost/max_boost range,
so the ACTUAL live selling price can drift below (or sit between)
price_min_elastic/price_max_elastic well after enrollment -- a one-time
check at enrollment time cannot catch this, since the live price keeps
moving on its own. This step re-checks every CURRENTLY ENROLLED product's
real live price (not the enrollment-time reference) every day.

Steps:
  1. For every active campaign, list currently-enrolled products and their
     real live action_price.
  2. Compute each one's M&S-cost ratio against that live price (today's
     M&S price from price_history.jsonl).
  3. Anything > 46% is removed (POST /v1/actions/products/deactivate) and
     logged to margin_risk_removals.jsonl.
  4. Re-homing: for every removed product, check every OTHER active
     campaign's candidate list -- if it appears there and the 46% rule
     passes at that campaign's own price, enroll it there instead
     (confirmed manually 2026-09-02: 0 of an earlier 288-product batch
     qualified anywhere else, but this makes that check automatic and
     product-specific rather than assuming the answer is always no).

Wired into daily_run.py as its own step, after enroll_campaigns.py.
"""
import json
import sys
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from check_auto_added_campaign_products import list_enrolled_products
from enroll_campaigns import list_active_campaigns, list_candidates
from margin_pricing import (
    compute_ratio_pct,
    fetch_usd_try_rate,
    get_real_stock,
    load_ms_prices,
    qualifies,
    resolve_offer_ids_and_names,
)
from ozon_client import call

REMOVAL_LOG = "margin_risk_removals.jsonl"


def deactivate(action_id, product_ids):
    if not product_ids:
        return
    for i in range(0, len(product_ids), 500):
        batch = product_ids[i:i + 500]
        call("/v1/actions/products/deactivate", {"action_id": action_id, "product_ids": batch})


def main():
    usd_try_rate = fetch_usd_try_rate()
    ms_prices = load_ms_prices()
    campaigns = list_active_campaigns()
    print(f"Live USD/TRY rate: {usd_try_rate}")
    print(f"{len(ms_prices)} offer_id(s) with a known M&S price today.")
    print(f"{len(campaigns)} campaign(s) to re-scan.\n")

    to_remove_by_campaign = {}  # action_id -> [product_id, ...]
    removed_details = []  # for re-homing + logging

    for campaign in campaigns:
        action_id = campaign["id"]
        title = campaign.get("title", "")[:60]
        enrolled = list_enrolled_products(action_id)
        product_ids = [p["id"] for p in enrolled]
        info_by_pid = resolve_offer_ids_and_names(product_ids)

        flagged = []
        for p in enrolled:
            pid = p["id"]
            offer_id, name = info_by_pid.get(pid, (None, None))
            if not offer_id:
                continue
            ms_entry = ms_prices.get(offer_id)
            if not ms_entry:
                continue
            ms_price_try, _url = ms_entry
            live_price = p.get("action_price", 0)
            ratio = compute_ratio_pct(ms_price_try, live_price, usd_try_rate)
            if ratio is not None and not qualifies(ratio):
                flagged.append({
                    "product_id": pid, "offer_id": offer_id, "name": name,
                    "action_id": action_id, "campaign_title": campaign.get("title", ""),
                    "live_price": live_price, "ratio_pct": round(ratio, 1),
                })

        print(f"Campaign {action_id} - {title}: {len(enrolled)} enrolled, {len(flagged)} above 46% at live price.")
        if flagged:
            to_remove_by_campaign[action_id] = [f["product_id"] for f in flagged]
            removed_details.extend(flagged)

    total_removed = sum(len(v) for v in to_remove_by_campaign.values())
    print(f"\n{total_removed} product(s) to remove across {len(to_remove_by_campaign)} campaign(s).\n")

    for action_id, product_ids in to_remove_by_campaign.items():
        deactivate(action_id, product_ids)
        print(f"Deactivated {len(product_ids)} product(s) from campaign {action_id}.")

    now = datetime.now(timezone.utc).isoformat()
    with open(REMOVAL_LOG, "a", encoding="utf-8") as f:
        for r in removed_details:
            f.write(json.dumps({**r, "timestamp": now}, ensure_ascii=False) + "\n")

    # Re-homing: check every removed product against every OTHER campaign's
    # candidates (business instruction, 2026-09-02).
    re_homed = []
    if removed_details:
        print("\nChecking removed products against other campaigns for re-homing...")
        removed_ids = {r["product_id"] for r in removed_details}
        removed_by_id = {r["product_id"]: r for r in removed_details}

        for campaign in campaigns:
            action_id = campaign["id"]
            candidates = list_candidates(action_id)
            candidates_by_id = {c["id"]: c for c in candidates}
            overlap_ids = removed_ids & set(candidates_by_id.keys())
            if not overlap_ids:
                continue

            offer_ids = [removed_by_id[pid]["offer_id"] for pid in overlap_ids]
            real_stock_by_offer = get_real_stock(offer_ids)

            enrollments = []
            for pid in overlap_ids:
                # Don't re-home into the SAME campaign it was just removed from.
                if removed_by_id[pid]["action_id"] == action_id:
                    continue
                candidate = candidates_by_id[pid]
                offer_id = removed_by_id[pid]["offer_id"]
                real_stock = real_stock_by_offer.get(offer_id, 0)
                if real_stock <= 0:
                    continue
                ms_entry = ms_prices.get(offer_id)
                if not ms_entry:
                    continue
                ms_price_try, _url = ms_entry

                price_max_elastic = candidate.get("price_max_elastic")
                price_min_elastic = candidate.get("price_min_elastic")
                chosen_price = None
                if price_max_elastic and price_min_elastic:
                    for price in (price_max_elastic, price_min_elastic):
                        ratio = compute_ratio_pct(ms_price_try, price, usd_try_rate)
                        if qualifies(ratio):
                            chosen_price = price
                            break
                else:
                    max_action_price = candidate.get("max_action_price", 0)
                    if max_action_price:
                        ratio = compute_ratio_pct(ms_price_try, max_action_price, usd_try_rate)
                        if qualifies(ratio):
                            chosen_price = max_action_price

                if chosen_price is not None:
                    enrollments.append((pid, chosen_price, real_stock))

            if enrollments:
                from enroll_campaigns import enroll_products
                succeeded, rejected = enroll_products(action_id, enrollments)
                print(f"  Re-homed {len(succeeded)} product(s) into campaign {action_id} "
                      f"({campaign.get('title', '')[:40]}), {len(rejected)} rejected.")
                re_homed.extend(succeeded)

    print(f"\nDone. {total_removed} removed for thin margin, {len(re_homed)} re-homed into another "
          f"qualifying campaign, {total_removed - len(re_homed)} left unenrolled entirely.")


if __name__ == "__main__":
    main()
