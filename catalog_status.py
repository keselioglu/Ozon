"""
Read-only catalog snapshot for the dashboard / desktop widget -> catalog_status.json.

Per brand (M&S, H&M, other):
  live         offers listed on Ozon (not archived)
  url_ok       offers whose source page works: M&S page gave a price in the last 3 days /
               H&M page still exists in the latest H&M stock run
  for_sale     live offers with stock > 0 on Ozon right now
  price_fresh  offers whose price was pushed OK by a price run in the last 26 h
  stock_fresh  offers whose stock was pushed OK by a stock run in the last 26 h

The "fresh" numbers come from the run summaries (price_update_history.jsonl,
stock_update_history.jsonl, hm_stock_update_history.jsonl), capped at the live count.
Pushes nothing.
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from margin_pricing import get_real_stock
from warehouse_routing import MS_FAMILY_PREFIXES

OUT = Path(__file__).with_name("catalog_status.json")
HM_RE = re.compile(r"^(HM\d?-|\d-HM-)")
FRESH_HOURS = 26
URL_DAYS = 3


def brand(oid):
    if oid.startswith(MS_FAMILY_PREFIXES):
        return "M&S"
    if HM_RE.match(oid):
        return "H&M"
    return "Other"


def live_offer_ids():
    ids, last = [], ""
    while True:
        params = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if last:
            params["last_id"] = last
        res = call("/v3/product/list", params)["result"]
        ids += [i["offer_id"] for i in res["items"] if not i.get("archived")]
        last = res.get("last_id")
        if not last or not res["items"]:
            return ids


def last_run(path, ok_key):
    """(when, ok_count) of the newest summary line, or (None, 0)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None, 0
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        when = r.get("finished_at") or r.get("date")
        return when, int(r.get(ok_key) or 0)
    return None, 0


def is_fresh(when):
    if not when:
        return False
    try:
        t = datetime.fromisoformat(when)
    except ValueError:
        return False
    if len(when) == 10:  # date only -> count the whole day
        t += timedelta(hours=23, minutes=59)
    return datetime.now() - t <= timedelta(hours=FRESH_HOURS)


def working_url_offer_ids():
    """M&S: offer got a price from its own page in the last URL_DAYS days (price_history.jsonl).
    H&M: offer's H&M page still exists in the latest H&M stock run (hm_stock_last_run.json)."""
    ok = set()
    cutoff = (datetime.now() - timedelta(days=URL_DAYS - 1)).date().isoformat()
    try:
        with open("price_history.jsonl", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 15_000_000))
            for line in f.read().decode("utf-8", "replace").splitlines()[1:]:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("date", "") >= cutoff and r.get("url") and r.get("offer_id"):
                    ok.add(r["offer_id"])
    except FileNotFoundError:
        pass
    try:
        hm = json.loads(Path("hm_stock_last_run.json").read_text(encoding="utf-8"))
        ok |= {o for o, d in hm.get("decisions", {}).items() if d.get("why") not in ("product_gone", "colour_gone")}
        # size_not_matched / read_failed offers aren't in "decisions" with a stock -> page existed unless read failed
    except (FileNotFoundError, ValueError):
        pass
    return ok


def main():
    ids = live_offer_ids()
    stock = get_real_stock(ids) or {}
    url_ok = working_url_offer_ids()
    b = {k: {"live": 0, "url_ok": 0, "for_sale": 0, "price_fresh": 0, "stock_fresh": 0} for k in ("M&S", "H&M", "Other")}
    for o in ids:
        x = b[brand(o)]
        x["live"] += 1
        x["url_ok"] += o in url_ok
        x["for_sale"] += (stock.get(o) or 0) > 0

    runs = {
        "price_ms": last_run("price_update_history.jsonl", "pushed_ok"),
        "stock_ms": last_run("stock_update_history.jsonl", "products_pushed_ok"),
        "stock_hm": last_run("hm_stock_update_history.jsonl", "push_rows_ok"),
    }
    for key, brand_name, field in (("price_ms", "M&S", "price_fresh"), ("stock_ms", "M&S", "stock_fresh"),
                                   ("stock_hm", "H&M", "stock_fresh")):
        when, n = runs[key]
        if is_fresh(when):
            b[brand_name][field] = min(n, b[brand_name]["live"])

    total = {k: sum(x[k] for x in b.values()) for k in ("live", "url_ok", "for_sale", "price_fresh", "stock_fresh")}
    out = {"built": datetime.now().isoformat(timespec="seconds"), "fresh_hours": FRESH_HOURS,
           "brands": b, "total": total,
           "runs": {k: {"when": w, "ok": n, "fresh": is_fresh(w)} for k, (w, n) in runs.items()}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
