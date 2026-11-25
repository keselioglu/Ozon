"""
Daily H&M stock refresh: reads H&M Turkey's own stock for every live H&M
product on Ozon and pushes it (business decision 2026-11-23):

    in stock on H&M          -> 5
    "few pieces left"         -> 1
    sold out                  -> 0
    removed from H&M          -> 0, then archived on Ozon (archived_hm_gone.jsonl)
    size not matched          -> untouched

H&M (Akamai) blocks plain HTTP clients and Playwright-launched browsers, so this
starts a normal Chrome with its own profile (hm_chrome_profile/) and remote
debugging, attaches over CDP, warms up on the home page and calls H&M's own
endpoints from inside that page:
  - product page  www2.hm.com/tr_tr/productpage.<article>.html  -> __NEXT_DATA__
    variations/<article>/sizes  (sizeCode <-> size name, e.g. ...003 = "S")
  - ofg.hm.com/pdh-availability/v1/product/tr/availability/<7-digit product>
    -> {"availability": [sizeCodes in stock], "fewPieceLeft": [...]}

offer_id format: HM-<10-digit article>[-<size>] (also HM1-/1-HM- variants).
No size in the offer_id = one-size product: in stock if any size of the article is.

Safety: dry run by default (--apply pushes). Products whose page/availability
could not be read are left untouched; if more than 20% of products fail to read
(e.g. H&M blocked us), nothing is pushed at all.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ozon_client import call
from refresh_live_stock import push_stock_updates, snapshot_ozon_stock, FAILED_OFFER_IDS
from warehouse_routing import build_routed_stock_updates
from archive_helpers import archive_offer_ids, archive_offer_ids_individually

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333
PROFILE = Path(__file__).with_name("hm_chrome_profile")
HM_RE = re.compile(r"^(HM\d?-|\d-HM-)")
STOCK_IN, STOCK_FEW, STOCK_OUT = 5, 1, 0
MAX_READ_FAIL_SHARE = 0.20
HISTORY_FILE = "hm_stock_update_history.jsonl"
DETAIL_FILE = "hm_stock_last_run.json"
ARCHIVE_LOG = "archived_hm_gone.jsonl"
PAUSE = 0.8


def live_hm_offer_ids():
    ids, last = [], ""
    while True:
        params = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if last:
            params["last_id"] = last
        res = call("/v3/product/list", params)["result"]
        ids += [i["offer_id"] for i in res["items"] if not i.get("archived") and HM_RE.match(i["offer_id"])]
        last = res.get("last_id")
        if not last or not res["items"]:
            return ids


def parse_offer(oid):
    m = re.search(r"(\d{10})(?:-(.+))?$", oid)
    if not m:
        return None, None
    return m.group(1), (m.group(2) or "").upper().replace(" ", "")


def norm_size(name):
    return (name or "").upper().replace(" ", "")


# Every fetch has a 30 s timeout: one hung request once stalled a whole run (2026-11-23).
JS_PAGE = """async (article) => {
  const c = new AbortController(); setTimeout(() => c.abort(), 30000);
  const r = await fetch(`/tr_tr/productpage.${article}.html`, {credentials: 'include', signal: c.signal});
  if (r.status !== 200) return {status: r.status};
  // H&M redirects a delisted article to a category page
  if (r.redirected && !r.url.includes('productpage.')) return {status: 404, redirect: r.url};
  const html = await r.text();
  const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\\s\\S]*?)<\\/script>/);
  if (!m) return {status: r.status, error: 'no __NEXT_DATA__'};
  try {
    const v = JSON.parse(m[1]).props.pageProps.productPageProps.aemData.productArticleDetails.variations || {};
    const out = {};
    for (const [art, d] of Object.entries(v)) out[art] = (d.sizes || []).map(s => [s.sizeCode, s.name]);
    return {status: 200, variations: out};
  } catch (e) { return {status: 200, error: 'parse: ' + e}; }
}"""

JS_AVAIL = """async (pn) => {
  const c = new AbortController(); setTimeout(() => c.abort(), 30000);
  const r = await fetch(`https://ofg.hm.com/pdh-availability/v1/product/tr/availability/${pn}`, {signal: c.signal});
  return {status: r.status, body: r.status === 200 ? await r.json() : null};
}"""


def read_hm(products):
    """products: {7-digit: [10-digit articles]} -> {7-digit: {"sizes": {article: {name: code}}, "avail": set, "few": set} | {"error": ...}}"""
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(exist_ok=True)
    proc = subprocess.Popen([CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                             "--no-first-run", "--no-default-browser-check", "--window-size=1100,800",
                             "--window-position=40,40", "about:blank"])
    out = {}
    try:
        # Wait until Chrome's debug port answers (a fixed 4 s sleep failed once on 2026-11-24
        # when Chrome started slowly); give up after 30 s and let connect_over_cdp report it.
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).close()
                break
            except OSError:
                time.sleep(0.5)
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
            page = b.contexts[0].pages[0]
            page.goto("https://www2.hm.com/tr_tr/index.html", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            for i, (pn, arts) in enumerate(sorted(products.items()), 1):
                res = {"sizes": {}, "avail": set(), "few": set()}
                try:
                    for art in arts:  # a delisted colour redirects; another colour may still have the page
                        pg = page.evaluate(JS_PAGE, art)
                        if pg["status"] != 404:
                            break
                        time.sleep(PAUSE)
                    if pg["status"] == 404:
                        res["gone"] = True
                    elif pg["status"] != 200 or "error" in pg:
                        res = {"error": f"page {pg['status']} {pg.get('error', '')}".strip()}
                    else:
                        res["sizes"] = {a: {norm_size(n): c for c, n in s} for a, s in pg["variations"].items()}
                    if "error" not in res:
                        av = page.evaluate(JS_AVAIL, pn)
                        if av["status"] == 404 and res.get("gone"):
                            pass
                        elif av["status"] != 200:
                            res = {"error": f"availability {av['status']}"}
                        else:
                            res["avail"] = set(av["body"].get("availability") or [])
                            res["few"] = set(av["body"].get("fewPieceLeft") or [])
                            # page gone but H&M's stock feed still lists sizes for our articles -> don't guess
                            if res.get("gone") and any(c[:10] in arts for c in res["avail"]):
                                res = {"error": "page gone but stock feed lists sizes"}
                except Exception as e:
                    res = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
                out[pn] = res
                if i % 25 == 0 or "error" in res:
                    print(f"  [{i}/{len(products)}] {pn}: {res.get('error') or ('gone' if res.get('gone') else 'ok')}")
                time.sleep(PAUSE)
            b.close()
    finally:
        proc.terminate()
    return out


def decide(oid, art, size, info):
    """-> (stock, reason) or (None, reason) to leave untouched."""
    if "error" in info:
        return None, "read_failed"
    if info.get("gone"):
        return STOCK_OUT, "product_gone"
    sizes = info["sizes"].get(art)
    if sizes is None:
        return STOCK_OUT, "colour_gone"
    if not size:
        codes = list(sizes.values())
    elif size in sizes:
        codes = [sizes[size]]
    else:
        # e.g. HM-0868220003-4: the "-4" is likely a pack count, not an H&M size name — don't guess
        return None, "size_not_matched"
    if any(c in info["avail"] and c not in info["few"] for c in codes):
        return STOCK_IN, "in_stock"
    if any(c in info["few"] for c in codes):
        return STOCK_FEW, "few_left"
    return STOCK_OUT, "sold_out"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="push to Ozon (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="only read the first N H&M products (testing)")
    args = ap.parse_args()

    offers = live_hm_offer_ids()
    parsed = {o: parse_offer(o) for o in offers}
    no_article = [o for o, (a, _) in parsed.items() if not a]
    products = defaultdict(set)
    for o, (a, _) in parsed.items():
        if a:
            products[a[:7]].add(a)
    products = {k: sorted(v) for k, v in sorted(products.items())}
    if args.limit:
        products = dict(list(products.items())[:args.limit])
    print(f"{len(offers):,} live H&M offer_id(s), {len(products):,} H&M product(s) to read"
          f"{f' (limited to {args.limit})' if args.limit else ''}; {len(no_article)} without a 10-digit article (untouched).")

    info = read_hm(products)
    failed = [pn for pn, r in info.items() if "error" in r]
    print(f"Read {len(info) - len(failed):,} of {len(info):,} H&M product(s); {len(failed)} failed.")

    decisions, reasons = {}, defaultdict(int)
    for o, (a, s) in parsed.items():
        if not a or a[:7] not in info:
            continue
        stock, why = decide(o, a, s, info[a[:7]])
        reasons[why] += 1
        if stock is not None:
            decisions[o] = {"stock": stock, "why": why}
    print("Decision per offer:", dict(reasons))
    Path(DETAIL_FILE).write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"),
                                             "decisions": decisions, "read_failed_products": failed},
                                            ensure_ascii=False, indent=1), encoding="utf-8")

    if info and len(failed) / len(info) > MAX_READ_FAIL_SHARE:
        print(f"ABORT: {len(failed)}/{len(info)} products could not be read (> {MAX_READ_FAIL_SHARE:.0%}) — "
              f"H&M may be blocking us. Nothing pushed.")
        return 2
    if not args.apply:
        print(f"DRY RUN — nothing pushed. Details in {DETAIL_FILE}.")
        return 0

    stock_map = {o: d["stock"] for o, d in decisions.items()}
    routed, skipped = build_routed_stock_updates(stock_map)
    before = snapshot_ozon_stock(stock_map.keys())
    ok, bad = push_stock_updates(routed)
    changed = sum(1 for o, v in stock_map.items() if before.get(o) is not None and before[o] != v)

    # Business rule 2026-11-23: offers removed from H&M (whole product or that colour) are archived.
    # Only after their stock is 0 on Ozon (Ozon refuses to archive items with warehouse stock).
    gone = sorted(o for o, d in decisions.items()
                  if d["why"] in ("product_gone", "colour_gone") and o not in FAILED_OFFER_IDS)
    archived, archive_blocked = 0, []
    if gone:
        print(f"\nArchiving {len(gone)} offer(s) removed from H&M (log: {ARCHIVE_LOG})...")
        archived, archive_failed, _ = archive_offer_ids(gone, ARCHIVE_LOG)
        if archive_failed:
            ok_ind, archive_blocked, other = archive_offer_ids_individually(gone, ARCHIVE_LOG)
            archived += len(ok_ind)
            archive_blocked += [o for o, _ in other]
        print(f"Archived {archived}; {len(archive_blocked)} refused by Ozon (e.g. Ozon-warehouse stock).")

    summary = {"date": datetime.now().strftime("%Y-%m-%d"), "offers_live": len(offers),
               "products_read": len(info) - len(failed), "products_read_failed": len(failed),
               "decisions": dict(reasons), "routing_skipped": len(skipped or []),
               "push_rows_ok": ok, "push_rows_failed": bad, "products_push_failed": len(FAILED_OFFER_IDS),
               "stock_changed": changed, "archived_gone": archived, "archive_refused": len(archive_blocked)}
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary) + "\n")
    print("Done.", json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
