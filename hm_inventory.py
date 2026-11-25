"""
Walks H&M Turkey's women's sections (hm_catalog.HM_SECTIONS) and counts what could be added to
Ozon, per Ozon category -> hm_inventory.json. Read-only toward H&M and Ozon.

Also keeps the candidate queue (every article that passes the checks, in H&M's own listing
order = its relevance ranking), which run_new_products.py takes H&M products from.

    python hm_inventory.py                  all pages of every section (~10-20 min)
    python hm_inventory.py --max-pages 3    first 3 pages per section (quick)
    python hm_inventory.py --sections elbise,pantolon

Uses the H&M Chrome profile/port of refresh_hm_stock.py -- not while that job runs (02:00).
"""
import argparse
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import daily_plan
import hm_catalog as hc
from hm_site import HMBrowser, HMError

OUT_FILE = "hm_inventory.json"
MAX_PAGE_FAIL_SHARE = 0.2


def live_hm_articles():
    """10-digit articles of every H&M offer on Ozon (live or not) -- an article on Ozon is skipped."""
    from ozon_client import call
    from refresh_hm_stock import HM_RE, parse_offer
    arts, last = set(), ""
    while True:
        params = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if last:
            params["last_id"] = last
        res = call("/v3/product/list", params)["result"]
        for i in res["items"]:
            if HM_RE.match(i["offer_id"]):
                a, _ = parse_offer(i["offer_id"])
                if a:
                    arts.add(a)
        last = res.get("last_id")
        if not last or not res["items"]:
            return arts


def walk(sections, max_pages=0, log=print):
    """-> (records in listing order, per-section stats). Each article appears once."""
    records, seen, stats = [], set(), {}
    with HMBrowser() as hm:
        for path in sections:
            st = {"pages": 0, "pages_failed": 0, "hits": 0, "total_pages": None}
            try:
                hits, total = hm.listing(path, 1)
            except HMError as e:
                log(f"  ! {path}: {e}")
                st["pages_failed"] += 1
                stats[path] = st
                continue
            st["total_pages"] = total
            last = min(total, max_pages) if max_pages else total
            page = 1
            while True:
                st["pages"] += 1
                for h in hits:
                    st["hits"] += 1
                    art = h.get("articleCode")
                    if art and art not in seen:
                        seen.add(art)
                        rec = hc.summarize_hit(h)
                        rec["section"] = path
                        records.append(rec)
                page += 1
                if page > last:
                    break
                try:
                    hits, _ = hm.listing(path, page)
                except HMError as e:
                    log(f"  ! {path} page {page}: {e}")
                    st["pages_failed"] += 1
                    hits = []
            log(f"  {path.rsplit('/', 1)[-1]}: {st['pages']}/{total} page(s), {st['hits']} hit(s), "
                f"{len(records)} distinct article(s) so far")
            stats[path] = st
    return records, stats


def aggregate(records, live_articles):
    counts = defaultdict(lambda: {"products": 0, "offers": 0})
    skips = Counter()
    candidates = []
    live_prices = {}
    for rec in records:
        why = hc.skip_reason(rec, live_articles)
        if why == "already on Ozon" and rec.get("price_try"):
            live_prices[rec["article"]] = rec["price_try"]
        if why:
            skips[why] += 1
            continue
        label = daily_plan.category_label_for_type(rec["type_id"])
        if not label:
            skips["category not in the plan's list"] += 1
            continue
        rec["label"] = label
        rec["offers"] = len(hc.sellable_sizes(rec))
        counts[label]["products"] += 1
        counts[label]["offers"] += rec["offers"]
        candidates.append(rec)
    return dict(counts), dict(skips), candidates, live_prices


def write_atomic(data, path=OUT_FILE):
    fd, tmp = tempfile.mkstemp(prefix=".hm_inventory_", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def build(sections=None, max_pages=0, log=print):
    sections = sections or hc.HM_SECTIONS
    log(f"Reading H&M sections ({len(sections)}){f', max {max_pages} page(s) each' if max_pages else ''}...")
    records, stats = walk(sections, max_pages, log)
    pages = sum(s["pages"] for s in stats.values())
    failed = sum(s["pages_failed"] for s in stats.values())
    live = live_hm_articles()
    counts, skips, candidates, live_prices = aggregate(records, live)
    return {"live_article_prices_try": live_prices,"walked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "complete": not max_pages and (pages == 0 or failed / max(pages, 1) <= MAX_PAGE_FAIL_SHARE),
            "max_pages": max_pages, "sections": stats, "articles_seen": len(records),
            "live_hm_articles": len(live), "counts": counts, "skips": skips, "candidates": candidates}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--sections", help="comma list of section names, e.g. elbise,pantolon")
    a = ap.parse_args(argv)
    secs = None
    if a.sections:
        want = {s.strip() for s in a.sections.split(",")}
        secs = [p for p in hc.HM_SECTIONS if p.rsplit("/", 1)[-1].removesuffix(".html") in want]
    data = build(secs, a.max_pages)
    write_atomic(data)
    print(f"\n{data['articles_seen']} article(s) seen, {len(data['candidates'])} addable; skipped: {data['skips']}")
    for label, c in sorted(data["counts"].items(), key=lambda kv: -kv[1]["offers"]):
        print(f"  {label:<15} {c['products']:>5} product(s) {c['offers']:>6} offer(s)")
    print(f"Saved {OUT_FILE}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
