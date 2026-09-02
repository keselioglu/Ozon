"""
Daily catalog-wide margin visibility report (business instruction,
2026-09-02: "check all live products with stock if the cost is higher than
46% even not in a campaign and report").

Unlike rescan_campaign_margins.py (which only touches campaign-enrolled
products and actively removes/re-homes them), this step is READ-ONLY and
covers the ENTIRE live catalog, campaign or not -- a product can be
thin-margin at its plain regular price with no campaign involved at all,
and nothing in this pipeline pushes price changes to Ozon, so there's
nothing to automatically fix here. This step exists purely for visibility.

Scoped to in-stock products only (business decision, 2026-09-02) -- an
out-of-stock listing's price isn't actionable today regardless of its
ratio.

Wired into daily_run.py as its own step, after rescan_campaign_margins.py.
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from margin_pricing import compute_ratio_pct, fetch_usd_try_rate, load_ms_prices, qualifies
from ozon_client import call

REPORT_FILE = "catalog_margin_risk_today.json"


def fetch_all_live_products():
    """offer_id -> product_id for every live product."""
    products = {}
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        for item in items:
            products[item["offer_id"]] = item["product_id"]
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return products


def fetch_prices_and_names(product_ids):
    """product_id -> {offer_id, name, price}."""
    info = {}
    product_ids = list(product_ids)
    for i in range(0, len(product_ids), 1000):
        batch = product_ids[i:i + 1000]
        result = call("/v3/product/info/list", {"product_id": batch})
        for item in result.get("items", []):
            info[item["id"]] = {
                "offer_id": item.get("offer_id"),
                "name": item.get("name"),
                "price": float(item.get("price") or 0),
            }
    return info


def fetch_real_stock(offer_ids):
    stock = {}
    offer_ids = [oid for oid in offer_ids if oid]
    for i in range(0, len(offer_ids), 100):
        batch = offer_ids[i:i + 100]
        result = call("/v4/product/info/stocks", {"filter": {"offer_id": batch, "visibility": "ALL"}, "limit": 100})
        for item in result.get("items", []):
            stock[item["offer_id"]] = sum(s.get("present", 0) for s in item.get("stocks", []))
    return stock


def fetch_active_campaign_prices():
    """product_id -> lowest active action_price across all campaigns (the
    real live selling price for enrolled products beats their regular
    price)."""
    from check_auto_added_campaign_products import list_enrolled_products
    from enroll_campaigns import list_active_campaigns

    active_price = {}
    for c in list_active_campaigns():
        for p in list_enrolled_products(c["id"]):
            pid = p["id"]
            action_price = p.get("action_price", 0)
            if action_price and (pid not in active_price or action_price < active_price[pid]):
                active_price[pid] = action_price
    return active_price


def main():
    usd_try_rate = fetch_usd_try_rate()
    print(f"Live USD/TRY rate: {usd_try_rate}")

    print("Fetching all live products...")
    live_products = fetch_all_live_products()
    print(f"  {len(live_products)} live products.\n")

    print("Fetching real stock...")
    stock_by_offer = fetch_real_stock(live_products.keys())
    in_stock_offer_ids = {oid for oid, s in stock_by_offer.items() if s > 0}
    print(f"  {len(in_stock_offer_ids)} in stock.\n")

    if not in_stock_offer_ids:
        print("Nothing in stock to check.")
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump({"checked": 0, "flagged_count": 0, "flagged": []}, f, ensure_ascii=False, indent=2)
        return

    print("Fetching prices and names...")
    in_stock_product_ids = [live_products[oid] for oid in in_stock_offer_ids]
    info_by_pid = fetch_prices_and_names(in_stock_product_ids)
    print(f"  {len(info_by_pid)} resolved.\n")

    print("Fetching active campaign prices...")
    campaign_price_by_pid = fetch_active_campaign_prices()
    print(f"  {len(campaign_price_by_pid)} have an active campaign price.\n")

    ms_prices = load_ms_prices()
    print(f"{len(ms_prices)} offer_id(s) with a known M&S price today.\n")

    flagged = []
    checked = 0
    for offer_id in in_stock_offer_ids:
        pid = live_products[offer_id]
        info = info_by_pid.get(pid)
        if not info:
            continue
        ms_entry = ms_prices.get(offer_id)
        if not ms_entry:
            continue
        ms_price_try, ms_url = ms_entry

        live_price = campaign_price_by_pid.get(pid, info["price"])
        if not live_price:
            continue

        checked += 1
        ratio = compute_ratio_pct(ms_price_try, live_price, usd_try_rate)
        if ratio is not None and not qualifies(ratio):
            flagged.append({
                "product_id": pid, "offer_id": offer_id, "name": info["name"],
                "live_price": live_price,
                "is_campaign_price": pid in campaign_price_by_pid,
                "regular_price": info["price"],
                "ms_price_try": ms_price_try,
                "ratio_pct": round(ratio, 1),
                "ms_url": ms_url,
                "real_stock": stock_by_offer.get(offer_id, 0),
            })

    flagged.sort(key=lambda r: -r["ratio_pct"])
    print(f"{checked} in-stock live product(s) had a known M&S price.")
    print(f"{len(flagged)} exceed the 46% margin threshold.\n")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "total_live": len(live_products),
            "total_in_stock": len(in_stock_offer_ids),
            "checked": checked,
            "flagged_count": len(flagged),
            "flagged": flagged,
        }, f, ensure_ascii=False, indent=2)

    print(f"Saved to {REPORT_FILE}.")
    print(f"\nDone. {len(live_products)} live, {len(in_stock_offer_ids)} in stock, "
          f"{checked} checked, {len(flagged)} flagged (ratio > 46%).")


if __name__ == "__main__":
    main()
