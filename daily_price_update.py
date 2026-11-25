"""
Daily Ozon price update for M&S-sourced products (Ozon Sales) — markdown ladder.

PRICE POLICY (business instruction, 2026-11-23; worked example cost 100):

    first price   = 4 x M&S cost  -> 400   set ONCE, when a product first enters
                                           the ladder (all live products on the
                                           first run, afterwards only new uploads).
                                           Never re-derived from cost later.
    not sold 2 wk -> first x 0.75  -> 300   (-25%)
    not sold 2 wk -> x 0.90        -> 270   (-25% then -10%)
    not sold 2 wk -> x 0.90        -> 243   (final step; stays here)

    old_price (crossed-out) = first price, so a markdown shows 400 -> 300.
    min_price (campaign limit) = current M&S cost / 0.48, refreshed daily.

"Not sold for 2 weeks" = 14 days IN STOCK on Ozon without a single non-
cancelled FBS/FBO order of that offer_id since the product reached its current
step. Out-of-stock days don't count (a product that can't be bought isn't
"not selling"). Any sale restarts the 14-day clock at the same price.

The price NEVER goes below the campaign limit (business rule, 2026-11-23): if
cost rises so far that cost / 0.48 > the ladder price, the price is held at
cost / 0.48 (counted as "held_at_campaign_limit"); old_price is raised to match
if needed, since Ozon rejects old_price < price.

ONE-TIME START (2026-11-23): on the very first run (empty ladder file) every
live product gets 4x AND a clock backdated 14 days, because they have been live
for a long time. So on the second run, products with no order in the last 14
days that are in stock drop straight to -25%. Products uploaded later start
a normal, fresh 14-day clock.

State lives in price_ladder_state.json (offer_id -> first_price, stage, ...),
written only on --apply and only for products Ozon accepted.

SAFETY — runs unattended at 05:00, so it refuses to push when inputs look wrong:
  * USD/TRY moved > RATE_MAX_MOVE vs the last successful run -> abort.
  * Fewer than MIN_FRESH_SHARE of live M&S products have a price captured
    today (refresh_prices.py failed / M&S unreachable) -> abort.
  * Sales or stock can't be read -> the API call raises -> nothing pushed.
  * Dry run by default. Only `--apply` writes to Ozon.

Appends one JSON line per applied run to price_update_history.jsonl (read by
the C:\\Git\\_dashboard dashboard).
"""
import argparse
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from margin_pricing import fetch_usd_try_rate, get_real_stock, load_ms_prices, PRICE_HISTORY_FILE
from ozon_client import call
from set_margin_floor_prices import fetch_all_prices
from warehouse_routing import MS_FAMILY_PREFIXES

FIRST_MULTIPLIER = 4.0                 # first price = 4 x cost
LADDER = [1.0, 0.75, 0.675, 0.6075]    # share of first price per step: 400, 300, 270, 243
STEP_DAYS = 14                         # in-stock days without a sale before the next step
FLOOR_RATIO = 0.48                     # min_price = cost / 0.48
SALES_LOOKBACK_DAYS = 16               # runs daily, so this only needs to cover a missed day or two
BATCH_SIZE = 100
RATE_MAX_MOVE = 0.15
MIN_FRESH_SHARE = 0.80
STATE_FILE = "price_update_state.json"
LADDER_FILE = "price_ladder_state.json"
HISTORY_FILE = "price_update_history.jsonl"
CANCELLED_STATUSES = {"cancelled"}
EPS = 0.01


def floor_price(cost_usd):
    return math.ceil(cost_usd / FLOOR_RATIO * 100) / 100


def ladder_price(first_price, stage):
    return round(first_price * LADDER[stage], 2)


def step(entry, today, in_stock, last_sale):
    """Advance one product's ladder entry by one day. Returns (new_entry, event)
    where event is None, "sold", or "drop". Pure — unit-tested."""
    e = dict(entry)
    if last_sale and last_sale >= e["stage_since"] and last_sale != e.get("last_sale_seen"):
        e.update(stage_since=today, in_stock_days=0, counted_on=today, last_sale_seen=last_sale)
        return e, "sold"
    if in_stock and e.get("counted_on") != today:
        e["in_stock_days"] = e.get("in_stock_days", 0) + 1
        e["counted_on"] = today
    if e["stage"] < len(LADDER) - 1 and e["in_stock_days"] >= STEP_DAYS:
        e.update(stage=e["stage"] + 1, stage_since=today, in_stock_days=0)
        return e, "drop"
    return e, None


def todays_price_offer_ids(path=PRICE_HISTORY_FILE):
    today = date.today().isoformat()
    ids = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if f'"date": "{today}"' in line:
                    try:
                        ids.add(json.loads(line)["offer_id"])
                    except (ValueError, KeyError):
                        pass
    except FileNotFoundError:
        pass
    return ids


def _postings(endpoint, since, to):
    """Cursor-paged posting list. The older /v3/posting/fbs/list (404) and
    /v2/posting/fbo/list ("obsolete method") are retired as of 2026-11-23."""
    out, cursor = [], ""
    while True:
        payload = {"dir": "ASC", "filter": {"since": since, "to": to}, "limit": 100,
                   "with": {"financial_data": False}}
        if cursor:
            payload["cursor"] = cursor
        r = call(endpoint, payload)
        res = r["result"] if isinstance(r.get("result"), dict) else r  # new endpoints: top-level postings/cursor
        out.extend(res.get("postings", []))
        cursor = res.get("cursor") or ""
        if not res.get("has_next") or not cursor:
            return out


def last_sale_dates(days=SALES_LOOKBACK_DAYS):
    """offer_id -> latest ISO date it was ordered (non-cancelled, FBS + FBO)."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    last = {}
    for p in _postings("/v4/posting/fbs/list", since, to) + _postings("/v3/posting/fbo/list", since, to):
        if p.get("status") in CANCELLED_STATUSES:
            continue
        d = (p.get("in_process_at") or p.get("created_at") or "")[:10]
        if not d:
            continue
        for item in p.get("products", []):
            oid = item.get("offer_id")
            if oid and d > last.get(oid, ""):
                last[oid] = d
    return last


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually push prices to Ozon (default: dry run)")
    ap.add_argument("--skip-freshness-check", action="store_true",
                    help="allow running on older M&S prices (for manual dry runs only)")
    args = ap.parse_args()
    today = date.today().isoformat()
    print(f"=== Daily price update — {'APPLY' if args.apply else 'DRY RUN'} — {datetime.now():%Y-%m-%d %H:%M} ===\n")

    rate = fetch_usd_try_rate()
    last_rate = load_json(STATE_FILE).get("last_rate")
    print(f"USD/TRY: {rate}" + (f" (last run: {last_rate})" if last_rate else ""))
    if last_rate and abs(rate - last_rate) / last_rate > RATE_MAX_MOVE:
        print(f"ABORT: USD/TRY moved more than {RATE_MAX_MOVE:.0%} since the last run — refusing to reprice on a suspicious rate.")
        return 2

    ms_prices = load_ms_prices()
    fresh = todays_price_offer_ids()
    print(f"{len(ms_prices):,} offer_id(s) with a known M&S price; {len(fresh):,} captured today.")

    print("Fetching current Ozon prices...")
    live = [i for i in fetch_all_prices() if i.get("offer_id", "").startswith(MS_FAMILY_PREFIXES)]
    print(f"{len(live):,} live M&S-family product(s).")

    with_price = [i for i in live if i["offer_id"] in ms_prices]
    fresh_share = (sum(i["offer_id"] in fresh for i in with_price) / len(with_price)) if with_price else 0
    print(f"Products with today's M&S price: {fresh_share:.0%} (need >= {MIN_FRESH_SHARE:.0%})")
    if fresh_share < MIN_FRESH_SHARE and not args.skip_freshness_check:
        print("ABORT: too few M&S prices refreshed today — refusing to price on stale costs.")
        return 3

    ladder = load_json(LADDER_FILE)
    seeding = not ladder  # very first run: all long-live products get a backdated clock (see below)
    print(f"{len(ladder):,} product(s) already on the price ladder."
          + (" First run: clocks backdated 14 days, so unsold products drop 25% tomorrow." if seeding else ""))
    print("Reading Ozon stock and the last {} days of orders...".format(SALES_LOOKBACK_DAYS))
    stock = get_real_stock([i["offer_id"] for i in with_price if i["offer_id"] in ladder])
    sales = last_sale_dates()
    print(f"{len(sales):,} offer_id(s) ordered in that window.\n")

    s = {"new_at_4x": 0, "drop_to_3x": 0, "drop_to_2_7x": 0, "drop_to_2_43x": 0, "sold_clock_reset": 0,
         "waiting": 0, "held_at_campaign_limit": 0, "skipped_no_ms_price": 0, "skipped_non_usd": 0}
    drop_keys = {1: "drop_to_3x", 2: "drop_to_2_7x", 3: "drop_to_2_43x"}
    updates, pending, examples = [], {}, []
    changed = {}  # offer_id -> (price_changed, limit_changed) vs. Ozon's current values; metrics only
    for item in live:
        oid = item["offer_id"]
        if oid not in ms_prices:
            s["skipped_no_ms_price"] += 1
            continue
        p = item.get("price", {})
        if p.get("currency_code") != "USD":
            s["skipped_non_usd"] += 1
            continue
        cost_usd = ms_prices[oid][0] / rate
        new_min = floor_price(cost_usd)

        if oid not in ladder:
            first = round(cost_usd * FIRST_MULTIPLIER, 2)
            if seeding:
                # ONE-TIME (business instruction 2026-11-23): these products have been
                # live for a long time, so the very first run backdates the clock 14 days
                # with 13 in-stock days already counted. Next day: no order in the last
                # 14 days + in stock -> straight to -25%; an order -> clock restarts.
                entry = {"first_price": first, "first_date": today, "stage": 0,
                         "stage_since": (date.today() - timedelta(days=STEP_DAYS)).isoformat(),
                         "in_stock_days": STEP_DAYS - 1, "counted_on": today, "backdated": True}
            else:
                entry = {"first_price": first, "first_date": today, "stage": 0, "stage_since": today,
                         "in_stock_days": 0, "counted_on": today}
            event = "new"
        else:
            entry, event = step(ladder[oid], today, stock.get(oid, 0) > 0, sales.get(oid))

        new_price = ladder_price(entry["first_price"], entry["stage"])
        if new_min > new_price:
            # Price never goes below the campaign limit: hold it AT the limit.
            new_price = new_min
            s["held_at_campaign_limit"] += 1
        new_old = max(entry["first_price"], new_price)  # Ozon rejects old_price < price
        if event == "new":
            s["new_at_4x"] += 1
        elif event == "drop":
            s[drop_keys[entry["stage"]]] += 1
        elif event == "sold":
            s["sold_clock_reset"] += 1
        else:
            s["waiting"] += 1

        pending[oid] = entry
        cur = (float(p.get("price") or 0), float(p.get("old_price") or 0), float(p.get("min_price") or 0))
        # Pushed EVERY day even when unchanged (business rule 2026-11-23): re-sending
        # the campaign limit makes Ozon re-check campaign membership — confirmed live
        # 2026-11-23, a product whose campaign price was below its limit was dropped
        # from 3 campaigns the moment its price + limit were pushed.
        updates.append({"offer_id": oid, "price": f"{new_price:.2f}", "old_price": f"{new_old:.2f}",
                        "min_price": f"{new_min:.2f}", "currency_code": "USD"})
        changed[oid] = (round(new_price, 2) != round(cur[0], 2), round(new_min, 2) != round(cur[2], 2))
        if event in ("new", "drop") and len(examples) < 8:
            examples.append((oid, event, round(cost_usd, 2), cur[0], new_price, new_min))

    print("Ladder:")
    print(f"  new at 4x cost        {s['new_at_4x']:,}")
    print(f"  dropped -25% (3x)     {s['drop_to_3x']:,}")
    print(f"  dropped -10% (2.7x)   {s['drop_to_2_7x']:,}")
    print(f"  dropped -10% (2.43x)  {s['drop_to_2_43x']:,}")
    print(f"  sold, clock restarted {s['sold_clock_reset']:,}")
    print(f"  waiting               {s['waiting']:,}")
    print(f"  held at campaign limit (ladder price was below cost/0.48) {s['held_at_campaign_limit']:,}")
    print(f"  skipped: {s['skipped_no_ms_price']:,} no M&S price, {s['skipped_non_usd']:,} non-USD")
    print(f"\n{len(updates):,} product(s) will be pushed (all of them, every day, so Ozon re-checks campaigns). Examples:")
    for e in examples:
        print(f"  {e[0]} [{e[1]}]: cost ${e[2]} · price {e[3]} -> {e[4]} · limit -> {e[5]}")

    ok = failed = 0
    failed_ids = set()
    if args.apply:
        if updates:
            print(f"\nPushing {len(updates):,} update(s) in batches of {BATCH_SIZE}...")
        for i in range(0, len(updates), BATCH_SIZE):
            batch = updates[i:i + BATCH_SIZE]
            for attempt in range(3):
                try:
                    result = call("/v1/product/import/prices", {"prices": batch})
                    break
                except RuntimeError as e:
                    if attempt == 2:
                        raise
                    print(f"  ! batch {i // BATCH_SIZE + 1} failed (attempt {attempt + 1}/3): {e} — retrying in 10s")
                    time.sleep(10)
            seen = set()
            for r in result.get("result", []):
                seen.add(r.get("offer_id"))
                if r.get("updated"):
                    ok += 1
                else:
                    failed += 1
                    failed_ids.add(r.get("offer_id"))
                    errs = "; ".join(e.get("message", str(e)) for e in r.get("errors", []))
                    print(f"  FAIL {r.get('offer_id')}: {errs}")
            failed_ids |= {u["offer_id"] for u in batch} - seen
        # Only remember ladder changes Ozon actually accepted; failures retry tomorrow.
        for oid, entry in pending.items():
            if oid not in failed_ids:
                ladder[oid] = entry
        with open(LADDER_FILE, "w", encoding="utf-8") as f:
            json.dump(ladder, f)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_rate": rate, "last_run": datetime.now().isoformat(timespec="seconds")}, f)
        # Metrics only: accepted offers whose price / campaign limit really changed
        # (every offer is re-pushed daily, so pushed_ok alone isn't "updated").
        price_changed_ok = sum(1 for oid, (pc, _) in changed.items() if pc and oid not in failed_ids)
        limit_changed_ok = sum(1 for oid, (_, lc) in changed.items() if lc and oid not in failed_ids)
        print(f"\nDone. {ok:,} products updated, {failed:,} failed, "
              f"{s['skipped_no_ms_price']:,} skipped (no M&S price).")
        print(f"{price_changed_ok:,} price(s) changed, {limit_changed_ok:,} campaign limit(s) changed.")
        rec = {"date": today, "finished_at": datetime.now().isoformat(timespec="seconds"),
               "usd_try": rate, "fresh_share": round(fresh_share, 3), "on_ladder": len(ladder),
               "needed_update": len(updates), "pushed_ok": ok, "push_failed": failed, **s,
               "price_changed_ok": price_changed_ok, "limit_changed_ok": limit_changed_ok}
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    else:
        print("\nDRY RUN — nothing was sent to Ozon. Re-run with --apply to push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
