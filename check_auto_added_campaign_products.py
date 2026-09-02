"""
Alerts (logs only, does NOT remove) on any campaign-enrolled product whose
add_mode isn't "MANUAL" -- business instruction (2026-09-02): "if any
product automatically is found remove these from the campaign and log it".

Held back from actually removing anything yet (business decision,
2026-09-02): Ozon's own add_mode field distinguishes "added automatically"
from "added manually by the seller" (confirmed via community-sourced Ozon
API client docs), but the exact string used for the automatic case was NOT
confirmed -- only that our own enrollments (via enroll_campaigns.py) show
"MANUAL". Rather than guess the removal trigger and risk reacting to the
wrong value, this script only DETECTS and LOGS any add_mode != "MANUAL" for
now, so the real value can be confirmed from an actual live occurrence
before wiring up removal. Ozon has campaigns with auto_add_dates already
scheduled (e.g. "Maximum Boosting: Deep Discounts", 48 products auto-adding
2026-09-07) -- check back after that date for a real example.

Once the real automatic-add value is confirmed from logged data here, add
the actual removal call (POST /v1/actions/products/deactivate) as a
follow-up -- do not add it before then.

Wired into daily_run.py as its own step (after enroll_campaigns.py).
"""
import json
import sys
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from enroll_campaigns import list_active_campaigns
from ozon_client import call

ALERT_LOG = "auto_added_campaign_products.jsonl"


def list_enrolled_products(action_id):
    products = []
    offset = 0
    while True:
        result = call("/v1/actions/products", {"action_id": action_id, "limit": 1000, "offset": offset})
        batch = result.get("result", {}).get("products", [])
        if not batch:
            break
        products.extend(batch)
        offset += 1000
    return products


def main():
    campaigns = list_active_campaigns()
    print(f"{len(campaigns)} campaign(s) to check.\n")

    total_checked = 0
    total_non_manual = 0

    for campaign in campaigns:
        action_id = campaign["id"]
        title = campaign.get("title", "")[:60]
        products = list_enrolled_products(action_id)
        total_checked += len(products)

        non_manual = [p for p in products if p.get("add_mode") != "MANUAL"]
        print(f"Campaign {action_id} - {title}: {len(products)} enrolled, "
              f"{len(non_manual)} non-MANUAL add_mode value(s).")

        if non_manual:
            product_ids = [p["id"] for p in non_manual]
            info = call("/v3/product/info/list", {"product_id": product_ids[:1000]})
            names_by_id = {item["id"]: (item.get("offer_id"), item.get("name"))
                           for item in info.get("result", {}).get("items", [])}

            with open(ALERT_LOG, "a", encoding="utf-8") as f:
                for p in non_manual:
                    offer_id, name = names_by_id.get(p["id"], (None, None))
                    entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "action_id": action_id, "campaign_title": campaign.get("title", ""),
                        "product_id": p["id"], "offer_id": offer_id, "name": name,
                        "add_mode": p.get("add_mode"),
                    }
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    print(f"  ALERT: {offer_id} ({name}) -> add_mode={p.get('add_mode')!r}")
            total_non_manual += len(non_manual)

    print(f"\nDone. {total_checked} enrolled product(s) checked across {len(campaigns)} campaign(s), "
          f"{total_non_manual} non-MANUAL add_mode value(s) found and logged to {ALERT_LOG}.")
    if total_non_manual:
        print("NOTE: not removed yet -- the real 'automatic' add_mode value needs confirming from this "
              "data before removal logic is safe to add (see module docstring).")


if __name__ == "__main__":
    main()
