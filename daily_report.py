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

Telegram summary (task #51): after the GitHub post, main() also sends a short
plain-text message (build_telegram_text(), ~10 lines) via
telegram_notify.send_telegram(): whether the 4am daily_run.py finished (read
from daily_run_status.json, see load_run_status()), which steps failed or
timed out, new offers vs. the daily plan, the content summary, and a few
snapshot numbers. If the metric gathering crashes, a minimal "Daily report
failed: <ExceptionType>" message (plus the run status lines) is still sent
and the exception is re-raised. Without TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
in .env, send_telegram() just logs "Telegram not sent: not configured".
Overview metrics (task #92): gather_report() also writes overview_metrics.json
(gitignored, written atomically by write_overview_metrics(), right after the
metrics are computed and before the GitHub post, so a gh failure doesn't lose
it) for the Ozon Sales app's Overview tab. A failure here never stops the
report. Shape:
  {"version": 1, "built_at": <ISO local time>,
   "campaigns": {live_skus (distinct enrolled product ids), ratio_count,
                 avg_ratio_pct, min_ratio_pct, max_ratio_pct},
   "sales": {window_from, window_to, revenue_usd, orders, units, skus,
             avg_ms_cost_pct, warehouse_pct {small, regular, unknown},
             brand_pct {M&S, H&M, Other}, top_category {label, units} | null,
             non_usd_skipped} | null}
Campaign ratios = M&S cost (TRY->USD) / action_price via compute_ratio_pct;
products with no M&S cost count in live_skus only. The sales window is
"34h": local midnight of yesterday until the run time (~10:00), sent to the
posting APIs in UTC (sales_window_34h()). summarize_sales() applies the same
rules as get_sales_window() (cancelled postings and non-USD items excluded,
avg_ms_cost_pct M&S-only and quantity-weighted). orders = non-cancelled
postings with at least one counted USD item; skus = distinct offer_ids sold.
Splits are by units: warehouse from the FBS posting's
delivery_method.warehouse_id when it is the small/regular warehouse, else
warehouse_assignments.json, else unknown; brand via
daily_plan.brand_for_offer_id(); category via products.csv article code ->
daily_plan label ("Unknown" if unresolved, only picked as top when nothing
else sold). Unknown values are null; splits are 0% when nothing sold. The
24h snapshot, markdown, Telegram text and daily_report_history.jsonl are
unchanged.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import shutil
import subprocess
from collections import Counter
from pathlib import Path

import telegram_notify
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
# Written by daily_run.py (write_run_status()), dated with daily_run's LOCAL date.
OVERVIEW_METRICS_FILE = "overview_metrics.json"
OVERVIEW_METRICS_VERSION = 1
RUN_STATUS_FILE = Path(__file__).resolve().parent / "daily_run_status.json"
TELEGRAM_MAX_PROBLEMS = 5

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


def get_campaign_metrics(usd_try_rate, ms_prices):
    """{live_skus, ratio_count, avg_ratio_pct, min_ratio_pct, max_ratio_pct}.
    live_skus = distinct enrolled product ids across every active campaign;
    the ratios cover enrolled entries whose offer has an M&S cost."""
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

    return {
        "live_skus": len(enrolled_ids),
        "ratio_count": len(ratios),
        "avg_ratio_pct": round(sum(ratios) / len(ratios), 1) if ratios else None,
        "min_ratio_pct": round(min(ratios), 1) if ratios else None,
        "max_ratio_pct": round(max(ratios), 1) if ratios else None,
    }


class CampaignStats(tuple):
    """The old (enrolled_count, avg_ratio_pct_or_None) tuple, plus the full
    get_campaign_metrics() dict as .metrics."""
    def __new__(cls, metrics):
        obj = super().__new__(cls, (metrics["live_skus"], metrics["avg_ratio_pct"]))
        obj.metrics = metrics
        return obj


def get_campaign_stats(usd_try_rate, ms_prices):
    """(enrolled_count, avg_ratio_pct_or_None); the full dict is on .metrics."""
    return CampaignStats(get_campaign_metrics(usd_try_rate, ms_prices))


def campaign_metrics_from_stats(stats):
    """The metrics dict behind a get_campaign_stats() result. A plain tuple
    (no .metrics) gives the count/avg with the other ratios unknown (null)."""
    metrics = getattr(stats, "metrics", None)
    if isinstance(metrics, dict):
        return metrics
    count, avg = stats
    return {"live_skus": count, "ratio_count": None, "avg_ratio_pct": avg,
            "min_ratio_pct": None, "max_ratio_pct": None}


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


def sales_window_34h(now=None):
    """(window_from, window_to) as aware LOCAL datetimes: local midnight of
    yesterday until now. Convert with .astimezone(timezone.utc) for the API."""
    now = (now or datetime.now()).astimezone()
    yesterday = (now - timedelta(days=1)).date()
    window_from = datetime(yesterday.year, yesterday.month, yesterday.day,
                           tzinfo=now.tzinfo)
    return window_from, now


def _pct_split(counts, keys):
    """{key: % of total} rounded to 0.1 with the largest-remainder method, so
    a non-empty split adds up to exactly 100.0. All 0.0 when total is 0."""
    total = sum(counts.get(k, 0) for k in keys)
    if total <= 0:
        return {k: 0.0 for k in keys}
    tenths = {k: counts.get(k, 0) * 1000 / total for k in keys}
    floors = {k: int(v) for k, v in tenths.items()}
    left = 1000 - sum(floors.values())
    # Biggest remainder first; ties (to float noise) go to the bigger count.
    order = sorted(keys, key=lambda k: (-round(tenths[k] - floors[k], 9), -counts.get(k, 0)))
    for k in order[:left]:
        floors[k] += 1
    return {k: floors[k] / 10 for k in keys}


def _warehouse_key(wid):
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    if wid == SMALL_WAREHOUSE_ID:
        return "small"
    if wid == REGULAR_WAREHOUSE_ID:
        return "regular"
    return None


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def summarize_sales(postings, ms_prices, usd_try_rate, assignments, article_labels,
                    window_from=None, window_to=None):
    """Pure 34h sales summary for overview_metrics.json (see module docstring).
    Same counting rules as get_sales_window(). Never raises on empty input."""
    import daily_plan
    from ozon_mapping import extract_article_code_from_offer_id

    ms_prices = ms_prices or {}
    assignments = assignments or {}
    article_labels = article_labels or {}

    revenue = 0.0
    units = 0
    orders = 0
    skus = set()
    ms_ratio_sum = 0.0
    ms_ratio_units = 0
    non_usd_skipped = 0
    wh_units = Counter()
    brand_units = Counter()
    cat_units = Counter()

    for posting in postings or []:
        if not isinstance(posting, dict) or posting.get("status") in CANCELLED_STATUSES:
            continue
        posting_wh = _warehouse_key(
            (posting.get("delivery_method") or {}).get("warehouse_id"))
        counted = False
        for item in posting.get("products") or []:
            quantity = item.get("quantity", 0) or 0
            if quantity <= 0:
                continue
            if item.get("currency_code") != "USD":
                non_usd_skipped += quantity
                continue
            counted = True
            price = float(item.get("price", 0) or 0)
            offer_id = item.get("offer_id") or ""
            units += quantity
            revenue += price * quantity
            if offer_id:
                skus.add(offer_id)

            wh = posting_wh or _warehouse_key(assignments.get(offer_id)) or "unknown"
            wh_units[wh] += quantity
            brand_units[daily_plan.brand_for_offer_id(offer_id)] += quantity
            code = extract_article_code_from_offer_id(offer_id) if offer_id else None
            label = article_labels.get(code) if code else None
            cat_units[label or "Unknown"] += quantity

            if is_ms_offer_id(offer_id) and usd_try_rate:
                ms_entry = ms_prices.get(offer_id)
                if ms_entry:
                    ratio = compute_ratio_pct(ms_entry[0], price, usd_try_rate)
                    if ratio is not None:
                        ms_ratio_sum += ratio * quantity
                        ms_ratio_units += quantity
        if counted:
            orders += 1

    top_category = None
    known = [(lbl, n) for lbl, n in cat_units.items() if lbl != "Unknown"]
    pool = known or list(cat_units.items())
    if pool:
        lbl, n = sorted(pool, key=lambda x: (-x[1], x[0]))[0]
        top_category = {"label": lbl, "units": n}

    return {
        "window_from": _iso(window_from),
        "window_to": _iso(window_to),
        "revenue_usd": round(revenue, 2),
        "orders": orders,
        "units": units,
        "skus": len(skus),
        "avg_ms_cost_pct": round(ms_ratio_sum / ms_ratio_units, 1) if ms_ratio_units else None,
        "warehouse_pct": _pct_split(wh_units, ("small", "regular", "unknown")),
        "brand_pct": _pct_split(brand_units, ("M&S", "H&M", "Other")),
        "top_category": top_category,
        "non_usd_skipped": non_usd_skipped,
    }


def get_sales_34h(ms_prices, usd_try_rate, now=None):
    """Fetches the 34h window's postings (read-only) and summarizes them."""
    import daily_plan
    window_from, window_to = sales_window_34h(now)
    utc_from = window_from.astimezone(timezone.utc)
    utc_to = window_to.astimezone(timezone.utc)
    postings = fetch_fbs_postings(utc_from, utc_to) + fetch_fbo_postings(utc_from, utc_to)
    labels = daily_plan._article_labels(daily_plan.PRODUCTS_CSV) if postings else {}
    return summarize_sales(postings, ms_prices, usd_try_rate, load_warehouse_assignments(),
                           labels, window_from, window_to)


def overview_metrics_path():
    """overview_metrics.json sits next to daily_report_history.jsonl."""
    return os.path.join(os.path.dirname(os.path.abspath(HISTORY_FILE)), OVERVIEW_METRICS_FILE)


def build_overview_metrics(campaigns, sales, now=None):
    return {
        "version": OVERVIEW_METRICS_VERSION,
        "built_at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
        "campaigns": campaigns,
        "sales": sales,
    }


def write_overview_metrics(campaigns, sales, path=OVERVIEW_METRICS_FILE, now=None):
    """Atomically writes overview_metrics.json (tmp file + os.replace)."""
    data = build_overview_metrics(campaigns, sales, now)
    path = os.path.abspath(path)
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return data


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


def load_run_status(today_str=None):
    """daily_run_status.json as a dict, or None if missing/corrupt. A status
    from another day is still returned: build_telegram_text() compares its
    date with today_str and reports it as 'no run today'. Never raises."""
    try:
        with open(RUN_STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _hhmm(iso):
    """'05:42' from an ISO timestamp, '?' if unparseable."""
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M")
    except Exception:
        return "?"


def _short(value, limit=150):
    s = " ".join(str(value).split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _status_is_today(run_status, today_str):
    return isinstance(run_status, dict) and run_status.get("date") == today_str


def _run_status_lines(run_status, today_str):
    """The 'Daily run: ...' line, plus a 'Problems: ...' line when steps
    failed or timed out. today_str is daily_run.py's LOCAL date."""
    if not _status_is_today(run_status, today_str):
        last = run_status.get("date") if isinstance(run_status, dict) else None
        return [f"Daily run: ❓ no run today (last status: {last or 'none'})"]

    steps = run_status.get("steps")
    problems = [st for st in (steps if isinstance(steps, list) else [])
                if isinstance(st, dict) and st.get("status") in ("failed", "timeout")]
    state = run_status.get("state")
    if state == "running":
        line = f"Daily run: ⏳ still running or crashed (started {_hhmm(run_status.get('started_at'))})"
    elif state in ("failed", "blocked"):
        attention = run_status.get("attention")
        reason = ((attention[0] if isinstance(attention, list) and attention else None)
                  or run_status.get("summary") or "?")
        line = f"Daily run: ❌ FAILED: {_short(reason)}"
    elif state == "completed":
        if problems:
            n = len(problems)
            line = f"Daily run: ⚠️ completed with {n} problem{'s' if n != 1 else ''}"
        else:
            line = f"Daily run: ✅ completed {_hhmm(run_status.get('finished_at'))}"
    else:
        line = f"Daily run: ❓ unknown state ({_short(state, 40)})"

    lines = [line]
    if problems:
        names = [f"{_short(st.get('name') or '?', 40)} ({st.get('status')})"
                 for st in problems[:TELEGRAM_MAX_PROBLEMS]]
        extra = len(problems) - TELEGRAM_MAX_PROBLEMS
        if extra > 0:
            names.append(f"+{extra} more")
        lines.append("Problems: " + ", ".join(names))
    return lines


def _plan_total(run_status, today_str):
    """Today's plan total from the run status, else from daily_plan.json; None if neither."""
    if _status_is_today(run_status, today_str) and run_status.get("plan_total") is not None:
        return run_status["plan_total"]
    try:
        import daily_plan
        return daily_plan.plan_for_date(daily_plan.load_plan(), None)["total"]
    except Exception:
        return None


def build_telegram_text(snapshot, run_status, today_str):
    """Short plain-text Telegram summary (about 10 lines, < 1000 chars).
    today_str is the LOCAL date (daily_run.py's TODAY). Missing values show
    '?'. Never raises."""
    try:
        snap = snapshot if isinstance(snapshot, dict) else {}
        lines = [f"Ozon daily report {today_str}"]
        lines += _run_status_lines(run_status, today_str)
        lines.append(f"New offers: {fmt(snap.get('products_added'))}/"
                     f"{fmt(_plan_total(run_status, today_str))} plan "
                     f"(quota left {fmt(snap.get('quota_left'))})")
        content = run_status.get("content_summary") if _status_is_today(run_status, today_str) else None
        content = _short(content, 200) if content else "content status unknown"
        lines.append(content[:1].upper() + content[1:])
        lines.append(f"Stock: {snap.get('stock_freshness_str') or '?'}")
        lines.append(f"Campaigns: {snap.get('campaign_str') or '?'}")
        revenue = snap.get("revenue_usd")
        if isinstance(revenue, (int, float)):
            revenue = round(revenue, 2)
        lines.append(f"Sales 24h: {fmt(snap.get('units_sold'))} units, {fmt(revenue)} USD")
        return "\n".join(lines)[:1000]
    except Exception as e:
        return f"Ozon daily report {today_str}\nTelegram summary could not be built ({type(e).__name__})"


def build_telegram_failure_text(exc, run_status, today_str):
    """Minimal message for when the metric gathering crashed. Never raises."""
    head = [f"Ozon daily report {today_str}", f"Daily report failed: {type(exc).__name__}"]
    try:
        return "\n".join(head + _run_status_lines(run_status, today_str))[:1000]
    except Exception:
        return "\n".join(head)


def main():
    # daily_run.py dates its status with the LOCAL date, so compare against that.
    local_today = datetime.now().strftime("%Y-%m-%d")
    run_status = load_run_status(local_today)
    try:
        report_md, snapshot = gather_report()
    except Exception as e:
        telegram_notify.send_telegram(build_telegram_failure_text(e, run_status, local_today))
        raise

    post_github_report(report_md)
    telegram_notify.send_telegram(build_telegram_text(snapshot, run_status, local_today))
    print("Done.")


def gather_report():
    """Collects every metric, saves today's snapshot and returns
    (report_md, snapshot)."""
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
    campaign_stats = get_campaign_stats(usd_try_rate, ms_prices)
    enrolled_count, avg_campaign_ratio = campaign_stats
    campaign_metrics = campaign_metrics_from_stats(campaign_stats)

    print("Catalog margin flagged count...")
    catalog_flagged = get_catalog_margin_flagged_count()

    print("Warehouse assignment counts...")
    small_wh_count, regular_wh_count = get_warehouse_counts()

    print("Sales (last 24h, via posting lists)...")
    sales_today = get_sales_window(now - timedelta(hours=24), now)

    print("Overview metrics (campaigns + 34h sales)...")
    try:
        sales_34h = None
        try:
            sales_34h = get_sales_34h(ms_prices, usd_try_rate)
        except Exception as e:
            print(f"  34h sales failed: {type(e).__name__}: {e}")
        write_overview_metrics(campaign_metrics, sales_34h, path=overview_metrics_path())
    except Exception as e:
        print(f"  overview_metrics.json not written: {type(e).__name__}: {e}")

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
    return report_md, snapshot


if __name__ == "__main__":
    main()
