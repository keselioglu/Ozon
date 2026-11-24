"""
Account snapshot: one local summary of what the Ozon account holds per brand.

Read-only and offline. This script never talks to Ozon or any other network
service; it only reads local state files that other scripts already write:

  catalog_status.json          (catalog_status.py) -- per-brand "live" and
                               "for_sale" offer counts.
  catalog_live_offer_ids.json  (catalog_status.py) -- {"built", "count",
                               "offer_ids": [...]}, the live offer_id list.
  warehouse_assignments.json   (warehouse_routing.py) -- offer_id ->
                               warehouse_id, self-tracked routing state.

Output, account_snapshot.json (gitignored, written atomically):
  {"version": 1, "built_at": ISO,
   "sources": {"catalog_status": {path, status, mtime, built},
               "warehouse_assignments": {path, status, mtime, count},
               "live_offer_ids": {path, status, mtime, built}},
   "brands": {"M&S"|"H&M"|"Other": {offers, for_sale, live_ids_count,
              products, articles, unparsed, sizes: {token: count}}},
   "total": {same keys},
   "offers_by_warehouse": {"total": {label: count},
                           "by_brand": {brand: {label: count}}}}

status is "ok", "missing" or "corrupt" (unreadable JSON, or JSON whose top
level isn't an object). Any field that can't be known from
the inputs is null rather than guessed. Products / articles / sizes come only
from offer_ids in the strict canonical form MS-<article>-<color>-<size>;
anything else (legacy numeric MS ids, MAR-/SML-/MARK- variants, H&M ids) is
counted as "unparsed".

Usage:  python account_snapshot.py [--print] [--out P] [--catalog-status P]
                                   [--warehouses P] [--live-ids P]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from daily_plan import brand_for_offer_id
from ozon_mapping import ARTICLE_CODE_IN_OFFER_ID_RE
from warehouse_routing import REGULAR_WAREHOUSE_ID, SMALL_WAREHOUSE_ID

OUT_FILE = "account_snapshot.json"
CATALOG_STATUS_FILE = "catalog_status.json"
WAREHOUSE_FILE = "warehouse_assignments.json"
LIVE_IDS_FILE = "catalog_live_offer_ids.json"
VERSION = 1
BRANDS = ("M&S", "H&M", "Other")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OFFER_ID_RE = re.compile(
    r"^MS-(?P<article>" + ARTICLE_CODE_IN_OFFER_ID_RE.pattern + r")-(?P<color>[^-].*?)-(?P<size>[^-]+)$")

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


def file_timestamp(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError, TypeError):
        return None


def parse_offer_id(oid):
    """{'article', 'color', 'size'} for a canonical MS-<article>-<color>-<size>
    offer_id, else None. Strict: no guessing for other forms."""
    if not isinstance(oid, str):
        return None
    m = OFFER_ID_RE.match(oid)
    if not m or not ARTICLE_CODE_IN_OFFER_ID_RE.fullmatch(m.group("article")):
        return None
    return {"article": m.group("article"), "color": m.group("color"), "size": m.group("size")}


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
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _live_ids_from(live_ids):
    """List of str offer_ids, or None when the input isn't usable."""
    if not isinstance(live_ids, dict):
        return None
    live_ids = live_ids.get("offer_ids")
    if not isinstance(live_ids, list):
        return None
    return [o for o in live_ids if isinstance(o, str)]


def _source(info, key):
    info = info if isinstance(info, dict) else {}
    v = info.get(key)
    return v if isinstance(v, dict) else {}


def build_snapshot(catalog_status, live_ids, assignments, sources, now=None):
    now = now or datetime.now()
    built_at = now.isoformat(timespec="seconds") if isinstance(now, datetime) else str(now)

    cs_ok = isinstance(catalog_status, dict)
    cs_brands = catalog_status.get("brands") if cs_ok else None
    cs_brands = cs_brands if isinstance(cs_brands, dict) else {}
    ids = _live_ids_from(live_ids)
    asg = assignments if isinstance(assignments, dict) else {}

    cs_src = _source(sources, "catalog_status")
    wh_src = _source(sources, "warehouse_assignments")
    li_src = _source(sources, "live_offer_ids")
    cs_built = catalog_status.get("built") if cs_ok else None
    li_built = live_ids.get("built") if isinstance(live_ids, dict) else None
    out_sources = {
        "catalog_status": {"path": cs_src.get("path"), "status": cs_src.get("status"),
                           "mtime": cs_src.get("mtime"),
                           "built": cs_built if isinstance(cs_built, str) else None},
        "warehouse_assignments": {"path": wh_src.get("path"), "status": wh_src.get("status"),
                                  "mtime": wh_src.get("mtime"),
                                  "count": len(assignments) if isinstance(assignments, dict) else None},
        "live_offer_ids": {"path": li_src.get("path"), "status": li_src.get("status"),
                           "mtime": li_src.get("mtime"),
                           "built": li_built if isinstance(li_built, str) else None},
    }

    brands = {}
    for b in BRANDS:
        bd = cs_brands.get(b) if cs_ok else None
        brands[b] = {"offers": _count_field(bd, "live") if cs_ok else None,
                     "for_sale": _count_field(bd, "for_sale") if cs_ok else None,
                     "live_ids_count": None, "products": None, "articles": None,
                     "unparsed": None, "sizes": None}

    total = {"offers": None, "for_sale": None, "live_ids_count": None, "products": None,
             "articles": None, "unparsed": None, "sizes": None}
    if cs_ok:
        cs_total = catalog_status.get("total")
        for key, src_key in (("offers", "live"), ("for_sale", "for_sale")):
            v = _count_field(cs_total, src_key)
            if v is None and not isinstance(cs_total, dict):
                vals = [brands[b][key] for b in BRANDS]
                v = sum(vals) if cs_brands and all(x is not None for x in vals) else None
            total[key] = v

    by_wh = None
    if ids is not None:
        per = {b: {"ids": 0, "products": set(), "articles": set(), "unparsed": 0,
                   "sizes": Counter(), "wh": Counter()} for b in BRANDS}
        for oid in ids:
            b = brand_for_offer_id(oid)
            p = per.get(b) or per["Other"]
            p["ids"] += 1
            parsed = parse_offer_id(oid)
            if parsed is None:
                p["unparsed"] += 1
            else:
                p["products"].add((parsed["article"], parsed["color"]))
                p["articles"].add(parsed["article"])
                p["sizes"][parsed["size"]] += 1
            p["wh"][warehouse_label(asg.get(oid))] += 1

        all_products, all_articles = set(), set()
        all_sizes, all_wh = Counter(), Counter()
        for b in BRANDS:
            p = per[b]
            brands[b].update({"live_ids_count": p["ids"], "products": len(p["products"]),
                              "articles": len(p["articles"]), "unparsed": p["unparsed"],
                              "sizes": dict(sorted(p["sizes"].items()))})
            all_products |= p["products"]
            all_articles |= p["articles"]
            all_sizes += p["sizes"]
            all_wh += p["wh"]
        total.update({"live_ids_count": len(ids), "products": len(all_products),
                      "articles": len(all_articles),
                      "unparsed": sum(per[b]["unparsed"] for b in BRANDS),
                      "sizes": dict(sorted(all_sizes.items()))})
        by_wh = {"total": dict(sorted(all_wh.items())),
                 "by_brand": {b: dict(sorted(per[b]["wh"].items())) for b in BRANDS}}

    return {"version": VERSION, "built_at": built_at, "sources": out_sources,
            "brands": brands, "total": total, "offers_by_warehouse": by_wh}


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


def format_summary(snap):
    lines = [f"Account snapshot {snap.get('built_at')}"]
    for b in BRANDS + ("total",):
        x = snap["total"] if b == "total" else snap["brands"][b]
        name = "Total" if b == "total" else b
        lines.append(f"  {name}: offers {_fmt(x['offers'])}, for sale {_fmt(x['for_sale'])}, "
                     f"products {_fmt(x['products'])}, articles {_fmt(x['articles'])}, "
                     f"unparsed {_fmt(x['unparsed'])}")
    wh = snap.get("offers_by_warehouse")
    if wh is None:
        lines.append("  Warehouses: unknown (no live offer_id list)")
    else:
        split = ", ".join(f"{k} {v}" for k, v in wh["total"].items()) or "none"
        lines.append(f"  Warehouses: {split}")
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
    args = ap.parse_args(argv)

    paths = {"catalog_status": _resolve(args.catalog_status),
             "warehouse_assignments": _resolve(args.warehouses),
             "live_offer_ids": _resolve(args.live_ids)}
    loaded, sources = {}, {}
    for key, p in paths.items():
        data, status = load_json_safe(p)
        if status == "ok" and not isinstance(data, dict):
            data, status = None, "corrupt"  # wrong top-level type: unusable, say so
        loaded[key] = data
        sources[key] = {"path": p, "status": status, "mtime": file_timestamp(p)}

    try:
        snap = build_snapshot(loaded["catalog_status"], loaded["live_offer_ids"],
                              loaded["warehouse_assignments"], sources)
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
