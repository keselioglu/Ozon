"""
10am daily business report (business instruction, 2026-09-02): a compact
Products + Sales snapshot, today vs. yesterday, posted to GitHub issue #13.

Row definitions (all confirmed with the business, 2026-09-02):
  Products added                    -- count of new_offer_ids submitted
                                        today via upload_to_ozon.py
                                        (new_items_submitted.json).
  Up to date stocks                 -- of all live products, how many have
                                        a resolvable M&S source URL (so
                                        their stock CAN be kept current by
                                        refresh_live_stock.py) vs. "unknown"
                                        (no source URL at all, stock can
                                        never be verified/refreshed).
  Product adding limit left         -- live daily_create quota remaining
                                        (upload_to_ozon.check_quota()).
  Products in campaigns             -- total enrolled (any campaign) vs.
                                        total live products.
  Live products with cost > 46%     -- from catalog_margin_report.py's
                                        flagged_count (catalog-wide, in
                                        stock, campaign or not).
  Average cost % of products in
    campaigns                       -- mean M&S-cost ratio across every
                                        currently-enrolled product with a
                                        resolvable M&S price.
  Total products sold / revenue     -- WHOLE ACCOUNT (business decision,
                                        2026-09-02: not scoped to M&S only),
                                        rolling 24h windows: "today" = last
                                        24h as of now, "yesterday" = the
                                        24h before that (business decision,
                                        2026-09-02, since a 10am run only
                                        has ~10h of true "today" data).
                                        Cancelled postings excluded
                                        (business decision, 2026-09-02).

                                        IMPORTANT (found live, 2026-09-02):
                                        /v1/analytics/data's "revenue"
                                        metric does NOT reliably convert to
                                        USD -- two different products gave
                                        two different implied conversion
                                        rates (68.96x and 73.20x against
                                        their known USD price), ruling out
                                        a single fixed-rate currency
                                        conversion. Sales data is instead
                                        built from /v3/posting/fbs/list +
                                        /v2/posting/fbo/list with
                                        with.financial_data=True, reading
                                        the TOP-LEVEL products[].price /
                                        currency_code (confirmed USD, and
                                        confirmed to match known product
                                        prices) -- NOT the nested
                                        financial_data.products[].currency_code,
                                        which showed "RUB" on the exact
                                        same numeric value as the top-level
                                        USD price for a real order (i.e.
                                        that nested currency_code appears
                                        to be mislabeled/boilerplate, not a
                                        real RUB amount).
  Average product cost % of
    products sold /
    sold with cost > 46%            -- M&S-PREFIXED sold items ONLY
                                        (business decision, 2026-09-02:
                                        "exclude from cost metrics, note
                                        the gap") -- non-M&S sales (other
                                        brands on this account) have no
                                        cost data and are excluded from
                                        these two rows specifically, with
                                        the excluded count noted.

"Yesterday" column values are NOT recomputed retroactively -- they're read
from this script's OWN stored history (daily_report_history.jsonl), so
"yesterday" always means "what this report said yesterday" (business
decision, 2026-09-02: "store each day's snapshot").

M&S offer_id prefixes recognized: MS-, MAR-, MARK-, SML-, MARKS- (same set
refresh_live_stock.py covers).

Scheduled via Windows Task Scheduler at 10:00am (separate from the
4am/5am/6am daily_run.py schedule) -- NOT wired into daily_run.py itself,
since this reads/summarizes state rather than acting on it.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import shutil
import subprocess

from check_auto_added_campaign_products import list_enrolled_products
from enroll_campaigns import list_active_campaigns
from margin_pricing import compute_ratio_pct, fetch_usd_try_rate, load_ms_prices, qualifies
from ozon_client import call
from refresh_live_stock import fetch_live_offer_ids_matching, load_legacy_url_map, load_pipeline_url_map
from upload_to_ozon import check_quota
from warehouse_routing import REGULAR_WAREHOUSE_ID, SMALL_WAREHOUSE_ID, load_warehouse_assignments

HISTORY_FILE = "daily_report_history.jsonl"
NEW_ITEMS_LOG = "new_items_submitted.json"
CATALOG_MARGIN_FILE = "catalog_margin_risk_today.json"

REPORT_REPO = "keselioglu/Ozon"
REPORT_ISSUE = 13
GH_EXE = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"

MS_PREFIXES = ("MS-", "MAR-", "MARK-", "SML-", "MARKS-")

# Postings in these statuses were never actually fulfilled -- excluded from
# "sold" (business decision, 2026-09-02: "exclude cancelled").
CANCELLED_STATUSES = {"cancelled"}


def is_ms_offer_id(offer_id):
    return bool(offer_id) and offer_id.startswith(MS_PREFIXES)


def get_products_added_today():
    try:
        with open(NEW_ITEMS_LOG, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return 0
    return len(data.get("new_offer_ids", []))


def get_stock_freshness():
    """(up_to_date_count, unknown_count, total_live) -- up_to_date means a
    resolvable M&S source URL exists (refreshable), unknown means it
    doesn't (can never be verified)."""
    cursor = ""
    total_live = 0
    all_offer_ids = []
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        total_live += len(items)
        all_offer_ids.extend(item.get("offer_id") for item in items)
        cursor = page.get("last_id")
        if not cursor or not items:
            break

    legacy_map = load_legacy_url_map()
    live_ms_ids = [oid for oid in all_offer_ids if is_ms_offer_id(oid)]
    pipeline_map = load_pipeline_url_map(live_ms_ids)
    url_for_offer_id = {**pipeline_map, **legacy_map}

    known_offer_ids, _ = fetch_live_offer_ids_matching(url_for_offer_id.keys())
    up_to_date = len(known_offer_ids)
    unknown = total_live - up_to_date
    return up_to_date, unknown, total_live


def get_campaign_stats(usd_try_rate, ms_prices):
    """(enrolled_count, avg_ratio_pct_or_None)."""
    campaigns = list_active_campaigns()
    enrolled_ids = set()
    all_enrolled = []
    for c in campaigns:
        products = list_enrolled_products(c["id"])
        all_enrolled.extend(products)
        for p in products:
            enrolled_ids.add(p["id"])

    ratios = []
    if enrolled_ids:
        product_ids = list(enrolled_ids)
        offer_by_pid = {}
        for i in range(0, len(product_ids), 1000):
            batch = product_ids[i:i + 1000]
            info = call("/v3/product/info/list", {"product_id": batch})
            for item in info.get("items", []):
                offer_by_pid[item["id"]] = item.get("offer_id")

        for p in all_enrolled:
            offer_id = offer_by_pid.get(p["id"])
            if not offer_id:
                continue
            ms_entry = ms_prices.get(offer_id)
            if not ms_entry:
                continue
            ms_price_try, _url = ms_entry
            ratio = compute_ratio_pct(ms_price_try, p.get("action_price", 0), usd_try_rate)
            if ratio is not None:
                ratios.append(ratio)

    avg_ratio = round(sum(ratios) / len(ratios), 1) if ratios else None
    return len(enrolled_ids), avg_ratio


def get_total_live_count():
    cursor = ""
    total = 0
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        total += len(items)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return total


def get_warehouse_counts():
    """(small_count, regular_count) from warehouse_routing.py's own
    last-run state (Ozon's stock-read endpoints don't reliably report
    current warehouse assignment back -- see warehouse_routing.py
    docstring, confirmed live 2026-09-02)."""
    assignments = load_warehouse_assignments()
    small = sum(1 for wid in assignments.values() if wid == SMALL_WAREHOUSE_ID)
    regular = sum(1 for wid in assignments.values() if wid == REGULAR_WAREHOUSE_ID)
    return small, regular


def get_catalog_margin_flagged_count():
    try:
        with open(CATALOG_MARGIN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("flagged_count", 0)
    except FileNotFoundError:
        return None


def fetch_fbs_postings(date_from, date_to):
    postings = []
    offset = 0
    while True:
        result = call("/v3/posting/fbs/list", {
            "filter": {
                "since": date_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": date_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "limit": 1000, "offset": offset,
            "with": {"financial_data": False},
        })
        batch = result.get("result", {}).get("postings", [])
        postings.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return postings


def fetch_fbo_postings(date_from, date_to):
    postings = []
    offset = 0
    while True:
        result = call("/v2/posting/fbo/list", {
            "filter": {
                "since": date_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": date_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "limit": 1000, "offset": offset,
            "with": {"financial_data": False},
        })
        batch = result.get("result", [])
        postings.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return postings


def get_sales_window(date_from, date_to):
    """Real per-order sales, whole account, in [date_from, date_to).
    Uses the TOP-LEVEL products[].price / currency_code from posting list
    endpoints (confirmed USD, matches known product prices) -- NOT the
    nested financial_data block, whose currency_code field does not
    reliably match the actual currency of its own numeric value (see
    module docstring)."""
    postings = fetch_fbs_postings(date_from, date_to) + fetch_fbo_postings(date_from, date_to)

    ms_prices = load_ms_prices()
    usd_try_rate = None

    total_units = 0
    total_revenue = 0.0
    ms_ratios_weighted = []
    ms_sold_count = 0
    ms_over_46_count = 0
    non_ms_excluded = 0
    non_usd_skipped = 0

    for posting in postings:
        if posting.get("status") in CANCELLED_STATUSES:
            continue
        for item in posting.get("products", []):
            quantity = item.get("quantity", 0)
            if quantity <= 0:
                continue
            if item.get("currency_code") != "USD":
                non_usd_skipped += quantity
                continue

            price = float(item.get("price", 0) or 0)
            total_units += quantity
            total_revenue += price * quantity

            offer_id = item.get("offer_id")
            if not is_ms_offer_id(offer_id):
                non_ms_excluded += quantity
                continue

            ms_entry = ms_prices.get(offer_id)
            if not ms_entry:
                continue
            if usd_try_rate is None:
                usd_try_rate = fetch_usd_try_rate()
            ms_price_try, _url = ms_entry
            ratio = compute_ratio_pct(ms_price_try, price, usd_try_rate)
            if ratio is None:
                continue
            ms_ratios_weighted.extend([ratio] * quantity)
            ms_sold_count += quantity
            if not qualifies(ratio):
                ms_over_46_count += quantity

    avg_ms_cost_pct = round(sum(ms_ratios_weighted) / len(ms_ratios_weighted), 1) if ms_ratios_weighted else None

    return {
        "units_sold": total_units,
        "revenue_usd": round(total_revenue, 2),
        "ms_sold_count": ms_sold_count,
        "avg_ms_cost_pct": avg_ms_cost_pct,
        "ms_sold_over_46_count": ms_over_46_count,
        "non_ms_excluded_count": non_ms_excluded,
        "non_usd_skipped_count": non_usd_skipped,
    }


def load_yesterday_snapshot():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return None
    if len(lines) < 1:
        return None
    return lines[-1]  # most recent prior snapshot = "yesterday" for today's report


def save_snapshot(snapshot):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def fmt(v, suffix=""):
    return "?" if v is None else f"{v}{suffix}"


def build_markdown(today, yesterday):
    def row(label, key, suffix="", fmt_pair=None):
        t = today.get(key)
        y = yesterday.get(key) if yesterday else None
        if fmt_pair:
            return f"| {label} | {fmt_pair(t)} | {fmt_pair(y) if yesterday else '—'} |"
        return f"| {label} | {fmt(t, suffix)} | {fmt(y, suffix) if yesterday else '—'} |"

    lines = [
        f"### Daily report: {today['date']}",
        "",
        "| Products | Today | Yesterday |",
        "|---|---|---|",
        row("Products added", "products_added"),
        row("Up to date stocks", "stock_freshness_str", fmt_pair=lambda v: v or "?"),
        row("Product adding limit left", "quota_left"),
        row("Products in campaigns", "campaign_str", fmt_pair=lambda v: v or "?"),
        row("Live products with cost > 46%", "catalog_flagged"),
        row("Average cost % of products in campaigns", "avg_campaign_ratio", suffix="%"),
        row("Products in Small Items warehouse", "small_warehouse_count"),
        row("Products in Ozpark (regular) warehouse", "regular_warehouse_count"),
        "",
        "| Sales (rolling 24h) | Today | Yesterday |",
        "|---|---|---|",
        row("Total products sold", "units_sold"),
        row("Total revenue", "revenue_usd", suffix=" USD"),
        row("Average product cost % of products sold (M&S only)", "avg_ms_cost_pct", suffix="%"),
        row("Products sold with cost > 46% (M&S only)", "ms_sold_over_46_count"),
    ]
    non_ms_note = today.get("non_ms_excluded_count", 0)
    non_usd_note = today.get("non_usd_skipped_count", 0)
    notes = []
    if non_ms_note:
        notes.append(f"{non_ms_note} non-M&S unit(s) sold in the last 24h excluded from the two cost rows above (no cost data tracked for other brands on this account)")
    if non_usd_note:
        notes.append(f"{non_usd_note} unit(s) skipped entirely (non-USD line item, could not be safely converted)")
    if notes:
        lines.append("")
        lines.append("_" + "; ".join(notes) + "._")
    return "\n".join(lines)


def post_github_report(body):
    body = f"@keselioglu\n\n{body}"
    try:
        result = subprocess.run(
            [GH_EXE, "issue", "comment", str(REPORT_ISSUE), "--repo", REPORT_REPO, "--body", body],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if result.returncode != 0:
            print(f"Could not post GitHub report: {result.stderr.strip()}")
        else:
            print("Posted daily report to GitHub issue #13.")
    except Exception as e:
        print(f"Could not post GitHub report: {e}")


def main():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    print("Fetching live USD/TRY rate and M&S prices...")
    usd_try_rate = fetch_usd_try_rate()
    ms_prices = load_ms_prices()

    print("Products added today...")
    products_added = get_products_added_today()

    print("Stock freshness...")
    up_to_date, unknown, total_live = get_stock_freshness()

    print("Quota remaining...")
    quota = check_quota()
    quota_left = None
    if quota:
        dc = quota.get("daily_create", {})
        quota_left = dc.get("limit", 0) - dc.get("usage", 0)

    print("Campaign stats...")
    enrolled_count, avg_campaign_ratio = get_campaign_stats(usd_try_rate, ms_prices)

    print("Catalog margin flagged count...")
    catalog_flagged = get_catalog_margin_flagged_count()

    print("Warehouse assignment counts...")
    small_wh_count, regular_wh_count = get_warehouse_counts()

    print("Sales (last 24h, via posting lists)...")
    sales_today = get_sales_window(now - timedelta(hours=24), now)

    snapshot = {
        "date": today_str,
        "products_added": products_added,
        "stock_freshness_str": f"{up_to_date} up to date / {unknown} unknown",
        "quota_left": quota_left,
        "campaign_str": f"{enrolled_count} in campaigns / {total_live} total live",
        "catalog_flagged": catalog_flagged,
        "small_warehouse_count": small_wh_count,
        "regular_warehouse_count": regular_wh_count,
        "avg_campaign_ratio": avg_campaign_ratio,
        "units_sold": sales_today["units_sold"],
        "revenue_usd": sales_today["revenue_usd"],
        "avg_ms_cost_pct": sales_today["avg_ms_cost_pct"],
        "ms_sold_over_46_count": sales_today["ms_sold_over_46_count"],
        "non_ms_excluded_count": sales_today["non_ms_excluded_count"],
        "non_usd_skipped_count": sales_today["non_usd_skipped_count"],
    }

    yesterday = load_yesterday_snapshot()
    save_snapshot(snapshot)

    report_md = build_markdown(snapshot, yesterday)
    print("\n" + report_md + "\n")

    post_github_report(report_md)
    print("Done.")


if __name__ == "__main__":
    main()
