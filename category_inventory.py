"""
Read-only pool snapshot per brand x category -> category_inventory.json.

For each M&S category (the Ozon product type daily_plan.CATEGORY_LABELS
names, same as the plan's allocation keys):
  live_offers                     live (not archived) Ozon offers whose article
                                  maps to this category via products.csv
  crawled_not_live_offers         in-stock crawled variants (products.csv) whose
                                  offer_id is not live, not deferred and not
                                  submitted today -- ready for an upload
  deferred_offers                 offers waiting in deferred_items.json
  upload_skips_7d                 distinct SKUs upload_to_ozon skipped in the
                                  last 7 days (upload_skipped.jsonl; only lines
                                  with a "date" field count -- older lines
                                  have none)
  discovered_not_live_products    products found on the M&S site but not live
  discovered_not_live_offers_est  their estimated in-stock offers
  last_discovery_walk             date of the walk those two numbers came from
  site_sections                   category_priority.csv sections feeding it

H&M has no add pipeline in this repo, so it only carries live / for_sale
copied from catalog_status.json, with add_pipeline: false.

Also a 14-day created-offers history (daily_created_history.jsonl, falling
back to daily_report_history.jsonl's products_added, today from
new_items_submitted.json).

Modes:
  python category_inventory.py         cheap: /v3/product/list +
                                       /v4/product/info/limit + local files,
                                       a minute or two
  python category_inventory.py --walk  SLOW (fetches every product page in
                                       every supported category): its own
                                       full read-only category walk for the
                                       discovered_* numbers. Queues nothing.

Never writes product_urls.txt, products.csv or deferred_items.json and
never calls a write endpoint. Writes category_inventory.json atomically and
holds category_inventory.refreshing while it runs. The "discovery_found"
key (written by category_priority.record_discovery_counts) is preserved.
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import created_history
import daily_plan
from ozon_mapping import (
    extract_article_code_from_offer_id, is_socks, map_size_to_eu, map_sock_size_to_offer_id_token,
)

OUT_FILE = "category_inventory.json"
MARKER_SUFFIX = ".refreshing"
PRODUCTS_CSV = "products.csv"
DEFERRED_FILE = "deferred_items.json"
SKIPPED_LOG = "upload_skipped.jsonl"
NEW_ITEMS_LOG = "new_items_submitted.json"
CATALOG_STATUS_FILE = "catalog_status.json"
REPORT_HISTORY_FILE = "daily_report_history.jsonl"
PRIORITY_FILE = "category_priority.csv"
URLS_FILE = "product_urls.txt"

MS = "M&S"
HM = "H&M"
SKIP_DAYS = 7
HISTORY_DAYS = 14
UNMAPPED = "Unmapped"
PARENT_SKU_RE = re.compile(r"\d{8,}")


# --- Ozon reads (read-only endpoints only) -------------------------------------

try:
    from catalog_status import live_offer_ids  # same paging as the catalog snapshot
except ImportError:
    def live_offer_ids():
        """Every non-archived offer_id on the account (/v3/product/list,
        read-only) -- same paging as catalog_status.live_offer_ids."""
        from ozon_client import call
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


def read_quota():
    """daily_create {limit, usage, remaining} from /v4/product/info/limit, or None."""
    try:
        from ozon_client import call
        dc = (call("/v4/product/info/limit", {}) or {}).get("daily_create") or {}
        limit, usage = dc.get("limit"), dc.get("usage")
        remaining = limit - usage if isinstance(limit, int) and isinstance(usage, int) else None
        return {"limit": limit, "usage": usage, "remaining": remaining, "reset_at": dc.get("reset_at")}
    except Exception as e:
        print(f"Could not read quota: {e}")
        return None


# --- Local file readers (all read-only, never raise) ---------------------------

def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_products_csv(path=PRODUCTS_CSV):
    """(rows, status). status is "ok", "missing" or "locked" (Excel has it
    open) -- rows is None unless ok."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f)), "ok"
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "locked"


def iter_jsonl_reverse(path, block=1 << 20):
    """Parsed records of a JSONL file from the LAST line backwards, reading
    blocks from the end so a huge log (upload_skipped.jsonl is hundreds of
    MB) costs only as much as the caller consumes. Bad lines are skipped."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        tail = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step) + tail
            lines = chunk.split(b"\n")
            tail = lines.pop(0)  # possibly partial -- completed by the next block
            for line in reversed(lines):
                rec = _parse_line(line)
                if rec is not None:
                    yield rec
        rec = _parse_line(tail)
        if rec is not None:
            yield rec


def _parse_line(line):
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def recent_skip_records(path=SKIPPED_LOG, today=None, days=SKIP_DAYS):
    """Skip records dated within the last `days` days (inclusive of today).
    The log is append-only and chronological, so reading stops at the first
    line (from the end) that is older or has no date at all."""
    cutoff = (_date.fromisoformat(today or daily_plan.utc_today()) - timedelta(days=days - 1)).isoformat()
    out = []
    for rec in iter_jsonl_reverse(path):
        d = rec.get("date")
        if not isinstance(d, str) or d < cutoff:
            break
        out.append(rec)
    return out


def report_history_added(path=REPORT_HISTORY_FILE):
    """{date: products_added} from daily_report_history.jsonl (10am snapshot)."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("products_added"), int):
                    out[rec.get("date")] = rec["products_added"]
    except OSError:
        pass
    return out


def load_site_sections(path=PRIORITY_FILE):
    """[{name, url, priority}] from category_priority.csv, file order."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return [{"name": r.get("Category Name (TR)"), "url": r.get("Full URL"),
                     "priority": r.get("Proprity")} for r in csv.DictReader(f)]
    except OSError:
        return []


# --- Pure helpers ---------------------------------------------------------------

def _positive_stock(row):
    v = row.get("stock_count")
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def row_offer_id(row):
    """The offer_id upload_to_ozon.build_ozon_item() would give this row
    (MS-{article}-{COLOR}-{size token}), or None if it can't be built (no
    article code or unmappable size). Pure -- no mapping log is written."""
    code = (row.get("ms_article_code") or "").strip()
    if not code:
        return None
    if is_socks(row.get("name")):
        token, _ = map_sock_size_to_offer_id_token(row.get("size_label"))
    else:
        token, _ = map_size_to_eu(row.get("size_label"))
    if not token:
        return None
    color = (row.get("color") or "").replace(" ", "").upper()
    return f"MS-{code}-{color}-{token}"


def _last_n_dates(today, n):
    t = _date.fromisoformat(today)
    return [(t - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def build_created_history(today, created_log, report_added, new_items_log, article_labels):
    """14 days of {date, created_total, by_category, source}, oldest first.
    Priority: new_items_submitted.json for today (most current), then
    daily_created_history.jsonl, then daily_report_history.jsonl
    (total only). No data -> created_total None."""
    out = []
    for d in _last_n_dates(today, HISTORY_DAYS):
        entry = {"date": d, "created_total": None, "by_category": None, "source": None}
        rec = created_log.get(d)
        if rec:
            entry.update(created_total=rec.get("created_total"),
                         by_category=rec.get("by_category"), source="daily_created_history")
        elif d in report_added:
            entry.update(created_total=report_added[d], source="daily_report_history")
        if (d == today and isinstance(new_items_log, dict) and new_items_log.get("date") == today):
            ids = [o for o in new_items_log.get("new_offer_ids") or [] if isinstance(o, str)]
            if len(ids) >= (entry["created_total"] or 0):
                by_cat = Counter()
                for oid in ids:
                    by_cat[article_labels.get(extract_article_code_from_offer_id(oid)) or UNMAPPED] += 1
                entry.update(created_total=len(ids), by_category=dict(sorted(by_cat.items())),
                             source="new_items_submitted")
        out.append(entry)
    return out


def aggregate(live_ids, product_rows, deferred_items, skip_records, discovery, hm_status,
              site_sections, label_for_name, today, created_log=None, report_added=None,
              new_items_log=None, quota=None):
    """Builds the snapshot dict from already-loaded inputs. Pure (no I/O).

    live_ids: iterable of live offer_ids. product_rows: products.csv dicts,
    or None if unreadable (category crawl numbers then stay 0 and the caller
    may carry over cached values). deferred_items: deferred_items.json list.
    skip_records: recent upload_skipped.jsonl dicts. discovery: the
    "discovery_found"-shaped dict {date, counts:{brand:{label:{products,
    offers}}}, sections?:{label:[...]}} or None. hm_status: catalog_status
    brands["H&M"] dict or None. site_sections: [{name, url, priority}].
    label_for_name: name -> category label or None."""
    labels = list(daily_plan.CATEGORY_LABELS.values())
    cats = {lbl: {"live_offers": 0, "crawled_not_live_offers": 0, "deferred_offers": 0,
                  "upload_skips_7d": 0, "discovered_not_live_products": 0,
                  "discovered_not_live_offers_est": 0, "last_discovery_walk": None,
                  "site_sections": []} for lbl in labels}
    ms = {"categories": cats, "live": 0, "live_unlabelled": 0, "unmapped_crawled": 0,
          "crawled_size_unmapped": 0, "deferred_unlabelled": 0, "discovered_unmapped_products": 0,
          "add_pipeline": True}

    name_cache = {}

    def label(name):
        if name not in name_cache:
            try:
                name_cache[name] = label_for_name(name) if name else None
            except Exception:
                name_cache[name] = None
        return name_cache[name]

    # article / parent_sku -> label, from crawled names
    article_labels, parent_labels = {}, {}
    for row in product_rows or []:
        lbl = label(row.get("name"))
        code = (row.get("ms_article_code") or "").strip()
        psku = str(row.get("parent_sku") or "").strip()
        if code and lbl and code not in article_labels:
            article_labels[code] = lbl
        if psku and lbl and psku not in parent_labels:
            parent_labels[psku] = lbl

    # live offers
    live = set(live_ids or [])
    for oid in live:
        if daily_plan.brand_for_offer_id(oid) != MS:
            continue
        ms["live"] += 1
        lbl = article_labels.get(extract_article_code_from_offer_id(oid))
        if not lbl:
            lbl = next((parent_labels[t] for t in PARENT_SKU_RE.findall(oid) if t in parent_labels), None)
        if lbl in cats:
            cats[lbl]["live_offers"] += 1
        else:
            ms["live_unlabelled"] += 1

    # deferred queue
    deferred_ids = set()
    for it in deferred_items or []:
        if not isinstance(it, dict):
            continue
        if it.get("offer_id"):
            deferred_ids.add(it["offer_id"])
        lbl = daily_plan.category_label_for_type(it.get("type_id"))
        if lbl in cats:
            cats[lbl]["deferred_offers"] += 1
        else:
            ms["deferred_unlabelled"] += 1

    # crawled, not live (and not already deferred / submitted today)
    submitted_today = set()
    if isinstance(new_items_log, dict) and new_items_log.get("date") == today:
        submitted_today = {o for o in new_items_log.get("new_offer_ids") or [] if isinstance(o, str)}
    seen = set()
    for row in product_rows or []:
        if not _positive_stock(row):
            continue
        lbl = label(row.get("name"))
        oid = row_offer_id(row)
        if oid is None:
            if lbl is None:
                ms["unmapped_crawled"] += 1
            else:
                ms["crawled_size_unmapped"] += 1
            continue
        if oid in seen:
            continue
        seen.add(oid)
        if oid in live or oid in deferred_ids or oid in submitted_today:
            continue
        if lbl in cats:
            cats[lbl]["crawled_not_live_offers"] += 1
        else:
            ms["unmapped_crawled"] += 1

    # upload skips, distinct SKUs per category
    skipped = {}
    for rec in skip_records or []:
        lbl = label(rec.get("name"))
        if lbl in cats:
            skipped.setdefault(lbl, set()).add(rec.get("sku"))
    for lbl, skus in skipped.items():
        cats[lbl]["upload_skips_7d"] = len(skus)

    # discovery counts
    if isinstance(discovery, dict):
        walk_date = discovery.get("date")
        for lbl, c in ((discovery.get("counts") or {}).get(MS) or {}).items():
            if lbl in cats:
                cats[lbl]["discovered_not_live_products"] = int(c.get("products") or 0)
                cats[lbl]["discovered_not_live_offers_est"] = int(c.get("offers") or 0)
            else:
                ms["discovered_unmapped_products"] += int(c.get("products") or 0)
        for lbl in cats:
            cats[lbl]["last_discovery_walk"] = walk_date

    # site sections: by section name, plus whatever a walk saw them come from
    for sec in site_sections or []:
        lbl = label(sec.get("name"))
        if lbl in cats:
            cats[lbl]["site_sections"].append(sec)
    if isinstance(discovery, dict):
        for lbl, secs in (discovery.get("sections") or {}).items():
            if lbl in cats:
                have = {s.get("url") for s in cats[lbl]["site_sections"]}
                cats[lbl]["site_sections"] += [s for s in secs if s.get("url") not in have]

    hm = {"live": None, "for_sale": None, "add_pipeline": False,
          "note": "no add pipeline yet -- stock refresh only"}
    if isinstance(hm_status, dict):
        hm["live"] = hm_status.get("live")
        hm["for_sale"] = hm_status.get("for_sale")

    return {
        "built": datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "quota": quota,
        "brands": {MS: ms, HM: hm},
        "created_history": build_created_history(today, created_log or {}, report_added or {},
                                                 new_items_log, article_labels),
    }


# --- --walk: own full read-only category walk ------------------------------------

def walk_discovery(label_for_name):
    """Walks every supported category_priority.csv section with the
    category_priority helpers, counting not-live products per category.
    Queues NOTHING: product_urls.txt is only read (to skip URLs already
    queued/crawled, same as daily discovery). Returns a discovery dict."""
    import category_priority as cp

    already = set(cp.load_line_set(URLS_FILE))  # local copy, never written back
    print("Fetching live MS-* offer_ids...")
    live = cp.fetch_live_ms_identifiers()
    counts, sections = {}, {}
    for row in cp.load_priority_categories(PRIORITY_FILE):
        name, url, prio = row.get("Category Name (TR)"), row.get("Full URL"), row.get("Proprity")
        if not cp.is_supported_category(name, url):
            continue
        print(f"[priority {prio}] {name} -- {url}")
        n = 0
        for item in cp.iter_new_products_in_category(url, live, already):
            already.add(item["url"])  # dedupe overlapping sections, in memory only
            lbl = label_for_name(item["name"]) or UNMAPPED
            c = counts.setdefault(lbl, {"products": 0, "offers": 0})
            c["products"] += 1
            c["offers"] += item["offers"]
            sec = {"name": name, "url": url, "priority": prio}
            if sec not in sections.setdefault(lbl, []):
                sections[lbl].append(sec)
            n += 1
        print(f"  -> {n} not-live product(s)")
    return {"date": daily_plan.utc_today(),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "category_inventory --walk",
            "counts": {MS: counts}, "sections": sections}


# --- Output ------------------------------------------------------------------------

def write_atomic(data, path=OUT_FILE):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".category_inventory_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


CRAWL_FIELDS = ("live_offers", "crawled_not_live_offers")


def build(walk=False):
    today = daily_plan.utc_today()
    previous = _read_json(OUT_FILE, {}) or {}
    label_for_name = daily_plan.category_label_for_name

    rows, csv_status = read_products_csv()
    print(f"products.csv: {csv_status} ({len(rows) if rows else 0} rows)")
    print("Fetching live offer_ids (/v3/product/list)...")
    live = live_offer_ids()
    print(f"{len(live)} live offer_id(s).")
    quota = read_quota()

    discovery = walk_discovery(label_for_name) if walk else previous.get("discovery_found")

    data = aggregate(
        live_ids=live, product_rows=rows,
        deferred_items=_read_json(DEFERRED_FILE, []),
        skip_records=recent_skip_records(today=today),
        discovery=discovery,
        hm_status=((_read_json(CATALOG_STATUS_FILE, {}) or {}).get("brands") or {}).get(HM),
        site_sections=load_site_sections(),
        label_for_name=label_for_name, today=today,
        created_log=created_history.load_history(),
        report_added=report_history_added(),
        new_items_log=_read_json(NEW_ITEMS_LOG),
        quota=quota,
    )
    data["mode"] = "walk" if walk else "default"
    data["sources"] = {"products_csv": csv_status}

    # products.csv locked/missing: carry over the last good crawl numbers.
    if rows is None:
        old_cats = (((previous.get("brands") or {}).get(MS) or {}).get("categories")) or {}
        for lbl, c in data["brands"][MS]["categories"].items():
            for k in CRAWL_FIELDS:
                if isinstance((old_cats.get(lbl) or {}).get(k), int):
                    c[k] = old_cats[lbl][k]
        data["sources"]["products_csv"] += " (crawl numbers carried over from the previous snapshot)"

    # Keep discovery_found -- category_priority.record_discovery_counts may
    # have written a newer one while we ran, so re-read just before writing.
    latest = (_read_json(OUT_FILE, {}) or {}).get("discovery_found")
    if walk:
        data["discovery_found"] = discovery
    elif latest is not None:
        data["discovery_found"] = latest
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--walk", action="store_true",
                    help="SLOW: full read-only category walk (fetches every product page); queues nothing")
    args = ap.parse_args(argv)

    marker = Path(OUT_FILE + MARKER_SUFFIX)
    marker.write_text(datetime.now().isoformat(), encoding="utf-8")
    try:
        data = build(walk=args.walk)
        write_atomic(data)
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    ms = data["brands"][MS]
    for lbl, c in ms["categories"].items():
        print(f"  {lbl:<14} live {c['live_offers']:>6}  crawled-not-live {c['crawled_not_live_offers']:>5}  "
              f"deferred {c['deferred_offers']:>4}  discovered {c['discovered_not_live_products']:>4}")
    print(f"Wrote {OUT_FILE}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
