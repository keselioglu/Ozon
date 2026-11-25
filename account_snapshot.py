"""
Account snapshot: one local summary of what the Ozon account holds per brand.

Read-only and offline. This script never talks to Ozon or any other network
service; it only reads local state files that other scripts already write:

  catalog_status.json          (catalog_status.py) -- per-brand "live" and
                               "for_sale" offer counts.
  catalog_live_offer_ids.json  (catalog_status.py) -- {"built", "count",
                               "offer_ids": [...]}, the live offer_id list.
  catalog_live_prices.json     (catalog_status.py) -- {"built", "count",
                               "prices": {offer_id: USD}}, each live SKU's
                               current final price (Ozon price.price).
  warehouse_assignments.json   (warehouse_routing.py) -- offer_id ->
                               warehouse_id, self-tracked routing state.
  overview_metrics.json        (daily_report.py) -- {"built_at", "campaigns",
                               "sales"}, copied through as they are.
  stock_update_history.jsonl   (refresh_live_stock.py) -- one M&S stock run
                               summary per line (finished_at,
                               products_pushed_ok).
  price_update_history.jsonl   (the M&S price job) -- one price run summary
                               per line (finished_at, pushed_ok).
  hm_stock_last_run.json       (the H&M stock job) -- {"date", "decisions":
                               {offer_id: {"stock", "why"}}}.

Output, account_snapshot.json (gitignored, written atomically):
  {"version": 2, "built_at": ISO,
   "sources": {catalog_status, live_offer_ids, warehouse_assignments,
               live_prices, overview_metrics, stock_history, price_history,
               hm_last_run: {path, status, mtime, ...}},
   "brands": {"M&S"|"H&M"|"Other": {offers, for_sale, live_ids_count,
              stock_up_to_date, price_up_to_date}},
   "total": {same keys},
   "up_to_date": {"hours": 24, "runs": {stock_ms, price_ms, stock_hm}},
   "notes": {"stock_up_to_date": {brand: text}, "price_up_to_date": {brand: text}},
   "warehouses": {label: {skus, by_brand: {brand: n}, price_count,
                          avg_price, min_price, max_price}} | null,
   "offers_by_warehouse": {"total": {label: count},
                           "by_brand": {brand: {label: count}}} | null,
   "campaigns": <overview_metrics.campaigns> | null,
   "sales": <overview_metrics.sales> | null,
   "metrics_built_at": <overview_metrics.built_at> | null}

"Up to date" = checked in the last UP_TO_DATE_HOURS (24) relative to `now`:
  M&S stock: newest stock_update_history line whose finished_at is within 24h
             -> min(products_pushed_ok, M&S live).
  H&M stock: hm_stock_last_run.json's date within 24h -> number of live H&M
             offer_ids with a numeric "stock" in its decisions.
  M&S price: newest price_update_history line whose finished_at is within 24h
             -> min(pushed_ok, M&S live).
  H&M/Other price and Other stock: 0, because there is no such run (see
  "notes"). Total = sum of the brands. A missing/corrupt source gives null
  (unknown); a readable source with no run inside the window gives 0. The
  jsonl files are read from their tail only; bad lines are skipped. A
  date-only stamp ("YYYY-MM-DD") counts as the end of that day, the same as
  catalog_status.py.

Warehouse price stats are USD, 2 decimals, over the live SKUs of that label
that have a valid price (number > 0) in catalog_live_prices.json. All four are
null when the price file is missing/corrupt; avg/min/max are null when no SKU
of that label has a valid price.

status is "ok", "missing" or "corrupt" (unreadable JSON, or JSON whose top
level isn't an object). Any field that can't be known from the inputs is null
rather than guessed. Bad inputs never raise.

Usage:  python account_snapshot.py [--print] [--out P] [--catalog-status P]
                                   [--warehouses P] [--live-ids P] [--prices P]
                                   [--overview-metrics P] [--stock-history P]
                                   [--price-history P] [--hm-last-run P]
"""
import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from daily_plan import brand_for_offer_id
from warehouse_routing import REGULAR_WAREHOUSE_ID, SMALL_WAREHOUSE_ID

OUT_FILE = "account_snapshot.json"
CATALOG_STATUS_FILE = "catalog_status.json"
WAREHOUSE_FILE = "warehouse_assignments.json"
LIVE_IDS_FILE = "catalog_live_offer_ids.json"
LIVE_PRICES_FILE = "catalog_live_prices.json"
OVERVIEW_METRICS_FILE = "overview_metrics.json"
STOCK_HISTORY_FILE = "stock_update_history.jsonl"
PRICE_HISTORY_FILE = "price_update_history.jsonl"
HM_LAST_RUN_FILE = "hm_stock_last_run.json"
VERSION = 2
BRANDS = ("M&S", "H&M", "Other")
UP_TO_DATE_HOURS = 24
JSONL_TAIL_BYTES = 2_000_000
NO_PRICE_RUN = "no price run"
NO_STOCK_RUN = "no stock run"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

UNASSIGNED = "Unassigned"


def load_json_safe(path):
    """(data, "ok") | (None, "missing") | (None, "corrupt"). Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "corrupt"
    except Exception:
        return None, "corrupt"


def load_jsonl_tail(path, max_bytes=JSONL_TAIL_BYTES):
    """(list of dict lines, status) from the last max_bytes of a jsonl file.
    Bad lines are skipped; the partial first line after a seek is dropped.
    "corrupt" when unreadable, or non-empty with no usable line. Never raises."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - max_bytes)
            f.seek(start)
            raw = f.read()
    except FileNotFoundError:
        return None, "missing"
    except Exception:
        return None, "corrupt"
    lines = raw.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    out, seen = [], 0
    for line in lines:
        if not line.strip():
            continue
        seen += 1
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    if seen and not out:
        return None, "corrupt"
    return out, "ok"


def file_timestamp(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError, TypeError):
        return None


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _valid_price(v):
    """float USD > 0, else None."""
    return float(v) if _is_number(v) and v > 0 else None


def _naive_local(t):
    return t.astimezone().replace(tzinfo=None) if t.tzinfo is not None else t


def parse_when(v):
    """Naive local datetime from an ISO string (a date-only value is the end
    of that day), or None when unusable."""
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    try:
        t = datetime.fromisoformat(v)
        if len(v) == 10:
            t += timedelta(hours=23, minutes=59, seconds=59)
        return _naive_local(t)
    except (ValueError, OverflowError, OSError):
        return None


def is_up_to_date(when, now, hours=UP_TO_DATE_HOURS):
    """True when `when` is at most `hours` before now (and not more than
    `hours` ahead of it, which would be a broken clock)."""
    t = parse_when(when)
    if t is None or not isinstance(now, datetime):
        return False
    age = _naive_local(now) - t
    return timedelta(hours=-hours) <= age <= timedelta(hours=hours)


def newest_run_in_window(records, ok_key, now, hours=UP_TO_DATE_HOURS):
    """{finished_at, ok} of the newest line with finished_at inside the window
    and a non-negative integer ok_key, else None. Bad lines are skipped."""
    best, best_t = None, None
    for r in records or ():
        if not isinstance(r, dict):
            continue
        n = r.get(ok_key)
        when = r.get("finished_at")
        if not _is_count(n) or n < 0 or not is_up_to_date(when, now, hours):
            continue
        t = parse_when(when)
        if best_t is None or t >= best_t:
            best, best_t = {"finished_at": when, "ok": n}, t
    return best


def warehouse_label(wid):
    if wid is None:
        return UNASSIGNED
    n = _as_int(wid)
    if n is not None:
        if n == SMALL_WAREHOUSE_ID:
            return "Small items"
        if n == REGULAR_WAREHOUSE_ID:
            return "Regular"
        return f"Other {n}"
    return f"Other {wid}"


def _count_field(d, key):
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    return v if _is_count(v) else None


def _live_ids_from(live_ids):
    """List of str offer_ids, or None when the input isn't usable."""
    if not isinstance(live_ids, dict):
        return None
    live_ids = live_ids.get("offer_ids")
    if not isinstance(live_ids, list):
        return None
    return [o for o in live_ids if isinstance(o, str)]


def _prices_from(prices):
    """{offer_id: float} of the valid prices, or None when the input isn't usable."""
    if not isinstance(prices, dict) or not isinstance(prices.get("prices"), dict):
        return None
    out = {}
    for oid, p in prices["prices"].items():
        p = _valid_price(p)
        if isinstance(oid, str) and p is not None:
            out[oid] = p
    return out


def _source(info, key):
    info = info if isinstance(info, dict) else {}
    v = info.get(key)
    return v if isinstance(v, dict) else {}


def _src(sources, key, **extra):
    s = _source(sources, key)
    out = {"path": s.get("path"), "status": s.get("status"), "mtime": s.get("mtime")}
    out.update(extra)
    return out


def _str_or_none(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, str) else None


def _sum_or_none(vals):
    return sum(vals) if all(_is_count(v) for v in vals) else None


def _price_stats(values):
    if not values:
        return {"price_count": 0, "avg_price": None, "min_price": None, "max_price": None}
    return {"price_count": len(values), "avg_price": round(sum(values) / len(values), 2),
            "min_price": round(min(values), 2), "max_price": round(max(values), 2)}


def build_snapshot(catalog_status, live_ids, assignments, sources, now=None, *,
                   prices=None, overview_metrics=None, stock_history=None,
                   price_history=None, hm_last_run=None):
    """Pure merge of the already-loaded inputs; never raises on bad inputs.
    None means "not available" (missing/corrupt). stock_history and
    price_history are lists of dict lines (see load_jsonl_tail)."""
    now = now if isinstance(now, datetime) else datetime.now()
    built_at = now.isoformat(timespec="seconds")

    cs_ok = isinstance(catalog_status, dict)
    cs_brands = catalog_status.get("brands") if cs_ok else None
    cs_brands = cs_brands if isinstance(cs_brands, dict) else {}
    ids = _live_ids_from(live_ids)
    asg = assignments if isinstance(assignments, dict) else {}
    price_map = _prices_from(prices)
    om = overview_metrics if isinstance(overview_metrics, dict) else None
    hm = hm_last_run if isinstance(hm_last_run, dict) else None
    stock_hist = stock_history if isinstance(stock_history, list) else None
    price_hist = price_history if isinstance(price_history, list) else None

    out_sources = {
        "catalog_status": _src(sources, "catalog_status", built=_str_or_none(catalog_status, "built")),
        "warehouse_assignments": _src(sources, "warehouse_assignments",
                                      count=len(assignments) if isinstance(assignments, dict) else None),
        "live_offer_ids": _src(sources, "live_offer_ids", built=_str_or_none(live_ids, "built")),
        "live_prices": _src(sources, "live_prices", built=_str_or_none(prices, "built"),
                            count=len(price_map) if price_map is not None else None),
        "overview_metrics": _src(sources, "overview_metrics", built=_str_or_none(om, "built_at")),
        "stock_history": _src(sources, "stock_history",
                              lines=len(stock_hist) if stock_hist is not None else None),
        "price_history": _src(sources, "price_history",
                              lines=len(price_hist) if price_hist is not None else None),
        "hm_last_run": _src(sources, "hm_last_run", date=_str_or_none(hm, "date")),
    }

    brands = {}
    for b in BRANDS:
        bd = cs_brands.get(b)
        brands[b] = {"offers": _count_field(bd, "live"), "for_sale": _count_field(bd, "for_sale"),
                     "live_ids_count": None, "stock_up_to_date": None, "price_up_to_date": None}

    total = {"offers": None, "for_sale": None, "live_ids_count": None,
             "stock_up_to_date": None, "price_up_to_date": None}
    if cs_ok:
        cs_total = catalog_status.get("total")
        for key, src_key in (("offers", "live"), ("for_sale", "for_sale")):
            v = _count_field(cs_total, src_key)
            if v is None and not isinstance(cs_total, dict):
                v = _sum_or_none([brands[b][key] for b in BRANDS]) if cs_brands else None
            total[key] = v

    # --- live ids: per-brand counts, warehouses -----------------------------------
    by_wh = warehouses = live_by_brand = None
    if ids is not None:
        live_by_brand = {b: [] for b in BRANDS}
        wh_counts = {b: Counter() for b in BRANDS}
        wh_prices = {}
        for oid in ids:
            b = brand_for_offer_id(oid)
            b = b if b in live_by_brand else "Other"
            live_by_brand[b].append(oid)
            label = warehouse_label(asg.get(oid))
            wh_counts[b][label] += 1
            p = price_map.get(oid) if price_map is not None else None
            if p is not None:
                wh_prices.setdefault(label, []).append(p)
        all_wh = Counter()
        for b in BRANDS:
            brands[b]["live_ids_count"] = len(live_by_brand[b])
            all_wh += wh_counts[b]
        total["live_ids_count"] = len(ids)
        by_wh = {"total": dict(sorted(all_wh.items())),
                 "by_brand": {b: dict(sorted(wh_counts[b].items())) for b in BRANDS}}
        warehouses = {}
        for label in sorted(all_wh):
            entry = {"skus": all_wh[label],
                     "by_brand": {b: wh_counts[b].get(label, 0) for b in BRANDS}}
            if price_map is None:
                entry.update({"price_count": None, "avg_price": None,
                              "min_price": None, "max_price": None})
            else:
                entry.update(_price_stats(wh_prices.get(label, [])))
            warehouses[label] = entry

    # --- up to date (last UP_TO_DATE_HOURS) -------------------------------------------
    ms_live = brands["M&S"]["offers"]
    if ms_live is None:
        ms_live = brands["M&S"]["live_ids_count"]

    runs = {"stock_ms": None, "price_ms": None, "stock_hm": None}
    for key, hist, ok_key, field in (("stock_ms", stock_hist, "products_pushed_ok", "stock_up_to_date"),
                                     ("price_ms", price_hist, "pushed_ok", "price_up_to_date")):
        if hist is None:
            continue  # source missing/corrupt: unknown
        run = newest_run_in_window(hist, ok_key, now)
        runs[key] = run
        if run is None:
            brands["M&S"][field] = 0
        elif ms_live is not None:
            brands["M&S"][field] = min(run["ok"], ms_live)

    if hm is not None:
        when = hm.get("date")
        fresh = is_up_to_date(when, now)
        decisions = hm.get("decisions")
        decisions = decisions if isinstance(decisions, dict) else {}
        n = None
        if not fresh:
            n = 0
        elif live_by_brand is not None:
            n = 0
            for oid in live_by_brand["H&M"]:
                d = decisions.get(oid)
                if isinstance(d, dict) and _is_number(d.get("stock")):
                    n += 1
        brands["H&M"]["stock_up_to_date"] = n
        runs["stock_hm"] = {"date": when if isinstance(when, str) else None, "fresh": fresh, "ok": n}

    # No price run exists for H&M / Other, and no stock run for Other: 0, not a guess.
    brands["Other"]["stock_up_to_date"] = 0
    brands["H&M"]["price_up_to_date"] = 0
    brands["Other"]["price_up_to_date"] = 0
    notes = {"stock_up_to_date": {"Other": NO_STOCK_RUN},
             "price_up_to_date": {"H&M": NO_PRICE_RUN, "Other": NO_PRICE_RUN}}
    for field in ("stock_up_to_date", "price_up_to_date"):
        total[field] = _sum_or_none([brands[b][field] for b in BRANDS])

    return {"version": VERSION, "built_at": built_at, "sources": out_sources,
            "brands": brands, "total": total,
            "up_to_date": {"hours": UP_TO_DATE_HOURS, "runs": runs}, "notes": notes,
            "warehouses": warehouses, "offers_by_warehouse": by_wh,
            "campaigns": om.get("campaigns") if om is not None else None,
            "sales": om.get("sales") if om is not None else None,
            "metrics_built_at": _str_or_none(om, "built_at")}


def write_atomic(path, data):
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    tmp = os.path.join(d, "." + os.path.basename(path) + ".tmp")
    try:
        text = json.dumps(data, ensure_ascii=False, indent=1)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _fmt(v):
    return "?" if v is None else str(v)


def _fmt_price(v):
    return "?" if v is None else f"${v:.2f}"


def format_summary(snap):
    lines = [f"Account snapshot {snap.get('built_at')}"]
    for b in BRANDS + ("total",):
        x = snap["total"] if b == "total" else snap["brands"][b]
        name = "Total" if b == "total" else b
        lines.append(f"  {name}: offers {_fmt(x.get('offers'))}, for sale {_fmt(x.get('for_sale'))}, "
                     f"stock up to date {_fmt(x.get('stock_up_to_date'))}, "
                     f"price up to date {_fmt(x.get('price_up_to_date'))}")
    wh = snap.get("warehouses")
    if wh is None:
        lines.append("  Warehouses: unknown (no live offer_id list)")
    elif not wh:
        lines.append("  Warehouses: none")
    else:
        for label, e in wh.items():
            if e.get("price_count") is None:
                price = "prices unknown"
            else:
                price = (f"{e['price_count']} priced, avg {_fmt_price(e.get('avg_price'))}, "
                         f"min {_fmt_price(e.get('min_price'))}, max {_fmt_price(e.get('max_price'))}")
            lines.append(f"  Warehouse {label}: {e.get('skus')} SKUs, {price}")
    camp = snap.get("campaigns")
    if isinstance(camp, dict):
        lines.append(f"  Campaigns: live SKUs {_fmt(camp.get('live_skus'))}, "
                     f"avg ratio {_fmt(camp.get('avg_ratio_pct'))}%")
    else:
        lines.append("  Campaigns: unknown")
    sales = snap.get("sales")
    if isinstance(sales, dict):
        lines.append(f"  Sales: orders {_fmt(sales.get('orders'))}, units {_fmt(sales.get('units'))}, "
                     f"revenue {_fmt(sales.get('revenue_usd'))} USD")
    else:
        lines.append("  Sales: unknown")
    bad = [f"{k} {v.get('status')}" for k, v in snap.get("sources", {}).items()
           if v.get("status") != "ok"]
    if bad:
        lines.append("  Sources not ok: " + ", ".join(bad))
    return "\n".join(lines)


def _resolve(p):
    return p if os.path.isabs(p) else os.path.join(SCRIPT_DIR, p)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline, read-only account snapshot.")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the summary only, write nothing")
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--catalog-status", default=CATALOG_STATUS_FILE)
    ap.add_argument("--warehouses", default=WAREHOUSE_FILE)
    ap.add_argument("--live-ids", default=LIVE_IDS_FILE)
    ap.add_argument("--prices", default=LIVE_PRICES_FILE)
    ap.add_argument("--overview-metrics", default=OVERVIEW_METRICS_FILE)
    ap.add_argument("--stock-history", default=STOCK_HISTORY_FILE)
    ap.add_argument("--price-history", default=PRICE_HISTORY_FILE)
    ap.add_argument("--hm-last-run", default=HM_LAST_RUN_FILE)
    args = ap.parse_args(argv)

    json_paths = {"catalog_status": _resolve(args.catalog_status),
                  "warehouse_assignments": _resolve(args.warehouses),
                  "live_offer_ids": _resolve(args.live_ids),
                  "live_prices": _resolve(args.prices),
                  "overview_metrics": _resolve(args.overview_metrics),
                  "hm_last_run": _resolve(args.hm_last_run)}
    jsonl_paths = {"stock_history": _resolve(args.stock_history),
                   "price_history": _resolve(args.price_history)}
    loaded, sources = {}, {}
    for key, p in json_paths.items():
        data, status = load_json_safe(p)
        if status == "ok" and not isinstance(data, dict):
            data, status = None, "corrupt"  # wrong top-level type: unusable, say so
        loaded[key] = data
        sources[key] = {"path": p, "status": status, "mtime": file_timestamp(p)}
    for key, p in jsonl_paths.items():
        loaded[key], status = load_jsonl_tail(p)
        sources[key] = {"path": p, "status": status, "mtime": file_timestamp(p)}

    try:
        snap = build_snapshot(loaded["catalog_status"], loaded["live_offer_ids"],
                              loaded["warehouse_assignments"], sources,
                              prices=loaded["live_prices"],
                              overview_metrics=loaded["overview_metrics"],
                              stock_history=loaded["stock_history"],
                              price_history=loaded["price_history"],
                              hm_last_run=loaded["hm_last_run"])
        print(format_summary(snap))
        if not args.print_only:
            out = _resolve(args.out)
            write_atomic(out, snap)
            print(f"Wrote {out}")
    except Exception as e:  # a bug, not an input problem
        print(f"account_snapshot failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
