"""
Daily "add new products to Ozon" job, M&S and H&M (scheduled task OzonSales_DailyNewProducts,
03:00 via run_new_products.bat). Business instruction 2026-11-25.

How many and which: daily_plan.json for today's UTC date (03:00 Moscow = 00:00 UTC, so the
job's day is the new UTC day). The owner sets it in the Ozon Sales app (Prioritization tab);
allocations are per brand x category ({"M&S": {"Pajama": 30}, "H&M": {"Dress": 20}}).
total 0 -> the job does nothing. The plan and the Ozon daily_create quota are hard caps;
whole products (all their in-stock sizes) are taken or left, never half a product.

Steps (each logged; any failure stops before the upload and is reported):
  1. plan + quota -> what is left to create today, per brand x category
  2. M&S: walk the M&S site sections of the planned categories (category_inventory.json),
     products not on Ozon -> crawl each page fresh (today's price and stock)
  3. H&M: read the planned H&M sections live (hm_site, same Chrome profile as the 02:00 H&M
     stock job) -> products not on Ozon, in H&M's own ranking order -> product page details
  4. pick whole products per category until the plan is covered; build a separate reserve of
     +15% (daily_plan.reserve_for) from products past their category's allocation -- used only
     to complete the day's total (short categories now, failed imports after step 8)
  5. translate only the picked products that have no translation yet (auto_translate)
  6. build Ozon items (same builders/rules as the existing uploads), drop offer_ids that
     already exist on Ozon and any offer_id that appears twice (ambiguous -> skip)
  7. upload (/v3/product/import), log to new_items_submitted.json / upload_tasks.jsonl
  8. wait for Ozon's import result, then push stock for the created offers ONLY (M&S: the
     fresh crawl count, H&M: 5 / 1 / 0) with the normal warehouse routing
  9. new_products_status.json + new_products_history.jsonl + a short Telegram message

    python run_new_products.py --dry-run     everything up to the upload, nothing written to
                                             Ozon or the state files, no translation calls
                                             (a placeholder translation tests the item build)
    python run_new_products.py               the real run (what the scheduled task does)
"""
import argparse
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

import daily_plan  # noqa: E402

STATUS_FILE = "new_products_status.json"
HISTORY_FILE = "new_products_history.jsonl"
DRYRUN_FILE = "new_products_dryrun.json"
LOCK_FILE = "new_products.lock"
OVERSHOOT = 1.5            # look at this many more offers than needed (checks drop some)
MS_OVERSHOOT = 3.0         # M&S loses more at build (already on Ozon, unmapped size/colour)
MAX_MS_PAGES_PER_LABEL = 60  # product pages fetched per M&S category at most
# Listing pages (36 products each) read per H&M section at most; a section stops as soon as the
# categories it supplies are covered. Was 6 -- test run 2026-11-27 14:06 came up 4 short (96/100)
# once the first 6 pages were already on Ozon from the morning's 500.
HM_PAGES_PER_SECTION = 25
IMPORT_WAIT_S = 900
HM_SECTIONS_FOR = {
    "Dress": ["elbise"], "Trousers": ["pantolon", "jean"], "Skirt": ["etek"], "Blouse": ["gomlek-bluz"],
    "Top": ["ust"], "T-Shirt": ["ust"], "Tank Top": ["ust"], "Sweater": ["hirka-kazak"],
    "Jacket": ["ceket-kaban", "blazer-yelek"], "Underwear": ["ic-camasir"], "Underwear Set": ["ic-camasir"],
    "Pajama": ["ev-kiyafetleri", "gecelik"], "Socks": ["corap-kulotlu-corap"],
}
PLACEHOLDER_TRANSLATION = {"name": "Проверка", "description": "Проверка", "material_id": None,
                           "material_text": "Проверка", "material_composition": "Состав: проверка",
                           "care_text": "Проверка", "hashtags": "#a #b #c #d #e"}


def log(msg=""):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --- 1. what is left to create today -------------------------------------------------------

def targets_for_today(today, test_products=None, test_offers=None):
    """-> (remaining dict from daily_plan.remaining_today, {(brand, label): offers wanted},
    open_brands) -- open_brands: brands that may take any category (fill_remainder with no
    allocation for them). test_products=N (owner's pre-run tests, 2026-11-26): a REAL run of
    about N whole products under the saved rules -- the day's number becomes the offers of the
    first N planned products and what was already created today is not counted."""
    from upload_to_ozon import check_quota
    import product_rules as pr
    rules, cached = None, {}
    if (test_products or test_offers) and not pr.rules_exist():
        raise RuntimeError("--test / --test-offers need the saved rules (new_product_rules.json)")
    if pr.rules_exist():
        # Owner rules (app: New Product Adding): daily number, price limits, priorities -- the
        # last saved ones decide now. The split per category comes from the cached data; the
        # site may differ a little, the reserve covers that.
        rules = pr.load_rules()
        candidates = pr.load_candidates()
        if test_products:
            first = pr.build_plan(dict(rules, daily_offers=max(rules["daily_offers"], 200)), candidates)
            first = sorted(first["products"], key=lambda p: -pr.WEIGHTS[p["level"]])[:test_products]
            rules = dict(rules, daily_offers=sum(p["offers"] for p in first) or test_products)
            log(f"TEST run: ~{test_products} products -> {rules['daily_offers']} offers (today's earlier "
                "uploads not counted)")
        elif test_offers:
            # owner's extra one-off test runs (2026-11-27): N offers under the saved priorities and
            # price limits; today's earlier uploads are not counted
            rules = dict(rules, daily_offers=test_offers)
            log(f"TEST run: {test_offers} offers (today's earlier uploads not counted)")
        day, preview = pr.plan_day_from_rules(rules, candidates)
        plan = {"version": 1, "default": day, "overrides": {}}
        for c in candidates:
            if not pr.price_block(c.get("cost_try"), rules):
                cached.setdefault((c["brand"], c["label"]), []).append(c)
        log(f"Rules: daily {rules['daily_offers']}, min sale ${rules['min_sale_usd']}, max cost "
            f"{rules['max_cost_try']} TL, non-medium priorities {rules.get('priorities') or '-'}; "
            f"cached plan {preview['total']} offers / {len(preview['products'])} products")
    else:
        plan = daily_plan.load_plan()
        day = daily_plan.plan_for_date(plan, today)
    quota = check_quota()
    q_left = None
    if quota:
        dc = quota.get("daily_create", {})
        q_left = dc.get("limit", 0) - dc.get("usage", 0)
    created_total, created_by_cat = daily_plan.created_today(today=today)
    if test_products or test_offers:
        created_total, created_by_cat = 0, {}
    remaining = daily_plan.remaining_today(plan, q_left, created_total, created_by_cat, today)
    log(f"Plan {today}: total {day['total']}, created so far {created_total}, quota left {q_left} "
        f"-> {remaining['total']} new offer(s) may be created; fill_remainder {remaining['fill_remainder']}")
    want = {k: n for k, n in remaining["per_category"].items() if n > 0}
    open_brands = set()
    if remaining["fill_remainder"] and remaining["total"] > sum(want.values()):
        open_brands = {"M&S", "H&M"}
    remaining["plan_total"] = day["total"]
    remaining["created_before"] = created_total
    remaining["quota_left"] = q_left
    remaining["rules"] = rules
    remaining["cached"] = cached
    return remaining, want, open_brands


def _weight(remaining, brand, label):
    import product_rules as pr
    rules = remaining.get("rules")
    return pr.WEIGHTS[pr.priority_of(rules, brand, label)] if rules else 0


def price_ok(cost_try, remaining):
    import product_rules as pr
    rules = remaining.get("rules")
    return rules is None or not pr.price_block(cost_try, rules)


def labels_for(brand, want, open_brands):
    labels = [lbl for (b, lbl) in want if b == brand]
    if brand in open_brands:
        labels += [lbl for lbl in daily_plan.CATEGORY_LABELS.values() if lbl not in labels]
    return labels


def needed(brand, label, want, remaining):
    """Offers to look for in one category: its allocation (or the day's total) plus the 15%
    reserve share, so spare products exist to complete the target."""
    n = want.get((brand, label)) or remaining["total"]
    return n + daily_plan.reserve_for(n)


# --- 2. M&S candidates -----------------------------------------------------------------------

def ms_sections(label):
    inv = {}
    try:
        with open("category_inventory.json", encoding="utf-8") as f:
            inv = json.load(f)
    except (OSError, ValueError):
        pass
    secs = ((inv.get("discovery_found") or {}).get("sections") or {}).get(label) or []
    if not secs:
        import category_priority as cp
        secs = [{"name": r.get("Category Name (TR)"), "url": r.get("Full URL"), "priority": r.get("Proprity")}
                for r in cp.load_priority_categories() if cp.is_supported_category(r.get("Category Name (TR)"), r.get("Full URL"))
                and daily_plan.category_label_for_name(r.get("Category Name (TR)")) == label]
    # Sections named after the category first (e.g. "Bluz" before a T-shirt section that happens
    # to hold a few blouses), then by priority.
    return sorted(secs, key=lambda s: (daily_plan.category_label_for_name(s.get("name") or "") != label,
                                       float(s.get("priority") or 9999)))


def ms_candidates(labels, want, remaining, report):
    """-> {label: [ {url, name, rows} ... ]} freshly crawled, not on Ozon, in priority order."""
    import category_priority as cp
    import crawler
    if not labels:
        return {}
    log(f"M&S: categories {labels}; reading live M&S offers on Ozon...")
    live = cp.fetch_live_ms_identifiers()
    seen = set(crawler.load_line_set("product_urls.txt"))  # already queued/crawled -> same as daily discovery
    out = {}
    for label in labels:
        goal = needed("M&S", label, want, remaining) * MS_OVERSHOOT
        got, fetched, picked = 0, 0, []
        # 1) the crawled products the app's plan shows (products.csv, not on Ozon), page re-read now
        for c in (remaining.get("cached") or {}).get(("M&S", label), []):
            if got >= goal or fetched >= MAX_MS_PAGES_PER_LABEL:
                break
            fetched += 1
            try:
                rows = crawler.extract_product(c["url"]) or []
            except Exception as e:
                log(f"    ! could not re-read {c['url']}: {e}")
                continue
            seen.add(c["url"])
            if not rows or cp.product_already_live(rows[0].get("ms_article_code"), rows[0].get("parent_sku"), live):
                continue
            n = sum(1 for r in rows if (r.get("stock_count") or 0) > 0)
            if n and price_ok(rows[0].get("price"), remaining):
                picked.append({"url": c["url"], "name": rows[0].get("name") or c["name"], "rows": rows})
                got += n
        if picked:
            log(f"  M&S {label}: {len(picked)} crawled product(s) still in stock (~{got} offer(s))")
        # 2) new products from the site sections
        for sec in ms_sections(label):
            if got >= goal or fetched >= MAX_MS_PAGES_PER_LABEL:
                break
            log(f"  M&S {label}: section {sec.get('name')} ({sec.get('url')})")
            for item in cp.iter_new_products_in_category(sec["url"], live, seen):
                seen.add(item["url"])
                if daily_plan.category_label_for_name(item["name"]) == label and item["offers"] > 0:
                    fetched += 1  # only real product-page fetches count; the name check is free
                    rows = crawler.extract_product(item["url"]) or []
                    if any((r.get("stock_count") or 0) > 0 for r in rows) and rows and \
                            price_ok(rows[0].get("price"), remaining):
                        picked.append({"url": item["url"], "name": item["name"], "rows": rows})
                        got += item["offers"]
                if got >= goal or fetched >= MAX_MS_PAGES_PER_LABEL:
                    break
        log(f"  M&S {label}: {len(picked)} product(s) found (~{got} offer(s)), {fetched} page(s) checked")
        report["ms_found"][label] = {"products": len(picked), "offers_est": got}
        out[label] = picked
    return out


# --- 3. H&M candidates -----------------------------------------------------------------------

def wait_for_hm_port(max_wait_s=1800):
    """The 02:00 H&M stock job uses the same Chrome port; wait if it's still running."""
    import urllib.request
    from refresh_hm_stock import PORT
    t0 = time.time()
    while time.time() - t0 < max_wait_s:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).close()
        except OSError:
            return True
        log("  H&M Chrome port busy (H&M stock job still running?) -- waiting 60 s")
        time.sleep(60)
    return False


def hm_candidates(labels, want, remaining, report, hm):
    """-> {label: [ {rec, detail} ... ]} not on Ozon, in H&M ranking order."""
    import hm_catalog as hc
    import hm_inventory as hi
    if not labels:
        return {}
    live = hi.live_hm_articles()
    sections = []
    for lbl in labels:
        for name in HM_SECTIONS_FOR.get(lbl, []):
            path = f"/tr_tr/kadin/urune-gore-satin-al/{name}.html"
            if path not in sections:
                sections.append(path)
    goal = {lbl: needed("H&M", lbl, want, remaining) * OVERSHOOT for lbl in labels}
    got = Counter()
    out = defaultdict(list)
    seen = set()
    skips = Counter()
    # labels each section can supply: a section is read only while one of them is still short
    serves = {p: [l for l in labels if p in {f"/tr_tr/kadin/urune-gore-satin-al/{n}.html"
                                             for n in HM_SECTIONS_FOR.get(l, [])}] for p in sections}
    for path in sections:
        page, total = 1, 1
        while page <= min(total, HM_PAGES_PER_SECTION):
            if all(got[l] >= goal[l] for l in serves[path] or labels):
                break
            try:
                hits, total = hm.listing(path, page)
            except Exception as e:  # 2026-11-27 03:00: one aborted page fetch killed the whole run
                log(f"  ! H&M {path.rsplit('/', 1)[-1]} page {page} unreadable, skipping the rest of "
                    f"this section: {str(e)[:160]}")
                skips["listing page unreadable"] += 1
                page += 1
                break
            page += 1
            for h in hits:
                rec = hc.summarize_hit(h)
                if rec["article"] in seen:
                    continue
                seen.add(rec["article"])
                why = hc.skip_reason(rec, live)
                if not why and not price_ok(rec.get("price_try"), remaining):
                    why = "outside the price limits"
                label = daily_plan.category_label_for_type(rec["type_id"]) if not why else None
                if why or label not in goal:
                    skips[why or "category not planned"] += 1
                    continue
                if got[label] >= goal[label]:
                    continue
                try:
                    detail = hm.product(rec["article"])
                except Exception as e:
                    log(f"  ! H&M product {rec['article']} unreadable: {str(e)[:120]}")
                    detail = None
                if not detail:
                    skips["product page gone"] += 1
                    continue
                out[label].append({"rec": rec, "detail": detail})
                got[label] += len(hc.sellable_sizes(rec))
        log(f"  H&M {path.rsplit('/', 1)[-1]}: read up to page {page - 1}/{total}")
    for lbl in labels:
        log(f"  H&M {lbl}: {len(out[lbl])} product(s) (~{got[lbl]} offer(s))")
        report["hm_found"][lbl] = {"products": len(out[lbl]), "offers_est": got[lbl]}
    report["hm_skips"] = dict(skips)
    return out


# --- 4-6. pick, translate, build -------------------------------------------------------------

def ms_items_for(product, translations, dry_run, dup_checks):
    import pandas as pd
    import upload_to_ozon as u
    from ozon_translations import PRODUCT_TRANSLATIONS
    live_codes, live_by_article = dup_checks
    items = []
    for row in product["rows"]:
        r = pd.Series(row)
        art = row.get("ms_article_code")
        if not art:
            continue
        if live_codes.get(art, set()) - {"MS"}:
            return [], "already live under another prefix"
        if u.is_color_duplicate_risk(art, row.get("color"), live_by_article):
            return [], "possible colour duplicate of a live listing"
        if art in translations:
            PRODUCT_TRANSLATIONS.setdefault(art, translations[art])
        item, _w = u.build_ozon_item(r)
        if item:
            item["_stock"] = int(row.get("stock_count") or 0)
            items.append(item)
    return items, None if items else "no size/colour could be mapped"


def translate(brand, article, name, specs_json, dry_run, client_box):
    from ozon_translations import PRODUCT_TRANSLATIONS
    if article in PRODUCT_TRANSLATIONS:
        return PRODUCT_TRANSLATIONS[article], None
    if dry_run:
        return dict(PLACEHOLDER_TRANSLATION), None
    import anthropic
    import auto_translate as at
    client_box.setdefault("c", anthropic.Anthropic())
    entry, err = at.generate_translation(article, name, specs_json, client=client_box["c"], brand=brand)
    if err:
        return None, err
    at.append_translation_to_file(article, entry)
    PRODUCT_TRANSLATIONS[article] = entry
    return entry, None


def pick_and_build(ms_found, hm_found, want, open_brands, remaining, dry_run, report):
    """Whole products, category by category, in priority order, until each category's
    allocation (and the day's total) is covered. Products past a category's allocation are
    built as the reserve (daily_plan.reserve_for, 15% of the day) -- kept separate and used
    only to complete the day's total: first for categories that came up short, later
    (run()) for offers Ozon did not import.
    -> (chosen, spares), both lists of (brand, label, items, source)."""
    import hm_catalog as hc
    import upload_to_ozon as u
    from upload_to_ozon import find_existing_offer_ids
    total_left = remaining["total"]
    reserve_left = daily_plan.reserve_for(remaining.get("plan_total") or remaining["total"])
    per_left = {k: v for k, v in want.items()}
    client_box = {}
    dup_checks = (u.fetch_live_article_codes_by_prefix(), u.fetch_live_offer_ids_by_article()) if ms_found else ({}, {})
    queues = [("M&S", lbl, ms_found.get(lbl, [])) for lbl in ms_found] + \
             [("H&M", lbl, hm_found.get(lbl, [])) for lbl in hm_found]
    # allocated categories first, then by the owner's priority (urgent ... low), then (fill_remainder) open ones
    rules = remaining.get("rules")
    if rules:
        import product_rules as pr
        weight = lambda b, l: pr.WEIGHTS[pr.priority_of(rules, b, l)]  # noqa: E731
    else:
        weight = lambda b, l: 0  # noqa: E731
    queues.sort(key=lambda q: (0 if (q[0], q[1]) in per_left else 1, -weight(q[0], q[1])))
    chosen, spares, skipped = [], [], Counter()
    later, overflow = [], []   # not built yet / built but too big for its category's share
    built_ids = set()

    def build(brand, label, p):
        """-> (entry, None) or (None, reason)."""
        if brand == "M&S":
            art = p["rows"][0].get("ms_article_code")
            specs = p["rows"][0].get("specs_json")
            tr, err = translate("M&S", art, p["name"], specs, dry_run, client_box) if art else (None, "no article")
            if not tr:
                return None, f"translation: {err}"[:80]
            items, why = ms_items_for(p, {art: tr}, dry_run, dup_checks)
        else:
            rec, det = p["rec"], p["detail"]
            tr, err = translate("H&M", rec["article"], rec["title"], hc.specs_for_translation(det), dry_run, client_box)
            if not tr:
                return None, f"translation: {err}"[:80]
            items = hc.build_items(rec, det, tr, u.TRY_TO_USD_RATE, u.package_dimensions(rec["title"]))
            why = None if items else "no image/colour/size"
        if why:
            return None, why
        if find_existing_offer_ids(it["offer_id"] for it in items):
            return None, "offer_id already exists on Ozon"
        ids = [it["offer_id"] for it in items]
        if len(set(ids)) != len(ids):
            return None, "two sizes map to the same offer_id (ambiguous)"
        # Real run 2026-11-27: M&S lists one article/colour under two page URLs (…452607, …452610)
        # -> the same offer_id was built twice, sent twice and counted twice.
        if built_ids.intersection(ids):
            return None, "same offer_id already built in this run (product on another page)"
        built_ids.update(ids)
        return (brand, label, items, p.get("url") or p.get("rec", {}).get("article")), None

    for brand, label, products in queues:
        key = (brand, label)
        for i, p in enumerate(products):
            cap = per_left.get(key)
            if cap is None and brand not in open_brands:
                break
            main_open = total_left > 0 and (cap is None or cap > 0)
            if not main_open and reserve_left <= 0:
                later.extend((brand, label, q) for q in products[i:])
                break
            entry, why = build(brand, label, p)
            if why:
                skipped[why] += 1
                continue
            n = len(entry[2])
            if main_open and n <= total_left and (cap is None or n <= cap):
                chosen.append(entry)
                total_left -= n
                if cap is not None:
                    per_left[key] = cap - n
            elif reserve_left > 0:
                spares.append(entry)
                reserve_left -= n
            else:
                overflow.append(entry)
    # Reserve, step 1: categories came up short -> complete the day's total from the spares.
    used = 0
    for entry in list(spares) + overflow:
        if total_left <= 0:
            break
        n = len(entry[2])
        if n <= total_left:
            chosen.append(entry)
            if entry in spares:
                spares.remove(entry)
            else:
                overflow.remove(entry)
            total_left -= n
            used += n
    # Still short (whole products / sold out / duplicates): keep building the products found in the
    # planned categories, best priority first, until the day's number is reached (+ the reserve).
    for brand, label, p in later:
        if total_left <= 0 and reserve_left <= 0:
            break
        entry, why = build(brand, label, p)
        if why:
            skipped[why] += 1
            continue
        n = len(entry[2])
        if 0 < total_left and n <= total_left:
            chosen.append(entry)
            total_left -= n
            used += n
        elif reserve_left > 0:
            spares.append(entry)
            reserve_left -= n
    if overflow:
        skipped["does not fit in what is left of the plan"] += len(overflow)
    report["skipped_at_build"] = dict(skipped)
    report["reserve"] = {"planned": daily_plan.reserve_for(remaining.get("plan_total") or remaining["total"]),
                         "built": used + sum(len(e[2]) for e in spares),
                         "used_for_shortfall": used, "used_for_failed_imports": 0}
    return chosen, spares


def take_spares(spares, n_needed):
    """Whole spare products (in order) adding up to at most n_needed offers; removes them
    from spares. -> list of entries."""
    out = []
    for entry in list(spares):
        n = len(entry[2])
        if n <= n_needed:
            out.append(entry)
            spares.remove(entry)
            n_needed -= n
        if n_needed <= 0:
            break
    return out


# --- 7-8. upload, wait, stock ---------------------------------------------------------------

def save_ms_rows(chosen_ms_products):
    """Persist the fresh M&S crawl rows (products.csv, product_urls.txt, processed_log.txt) so the
    01:00 stock job and the reports know these products."""
    import pandas as pd
    if not chosen_ms_products:
        return
    rows = [r for p in chosen_ms_products for r in p["rows"]]
    urls = {p["url"] for p in chosen_ms_products}
    df = pd.read_csv("products.csv", encoding="utf-8-sig", low_memory=False)
    df = pd.concat([df[~df["url"].isin(urls)], pd.DataFrame(rows)], ignore_index=True)
    tmp = "products.csv.tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, "products.csv")
    for f in ("product_urls.txt", "processed_log.txt"):
        with open(f, "a", encoding="utf-8") as fh:
            for url in sorted(urls):
                fh.write(url + "\n")


def log_new_items(items, today):
    from upload_to_ozon import NEW_ITEMS_LOG
    prior = {}
    try:
        with open(NEW_ITEMS_LOG, encoding="utf-8") as f:
            prior = json.load(f)
    except (OSError, ValueError):
        pass
    if prior.get("date") != today:
        prior = {"date": today, "quota_before": None, "new_offer_ids": [], "labels": {}}
    prior.setdefault("labels", {})
    for it in items:
        prior["new_offer_ids"].append(it["offer_id"])
        lbl = daily_plan.category_label_for_type(it.get("type_id"))
        if lbl:
            prior["labels"][it["offer_id"]] = lbl
    tmp = NEW_ITEMS_LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prior, f, ensure_ascii=False, indent=2)
    os.replace(tmp, NEW_ITEMS_LOG)


def upload(items):
    from ozon_client import call
    from upload_to_ozon import TASK_LOG
    task_ids = []
    for i in range(0, len(items), 100):
        batch = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items[i:i + 100]]
        r = call("/v3/product/import", {"items": batch})
        tid = (r.get("result") or {}).get("task_id")
        log(f"  batch {i // 100 + 1}: {len(batch)} item(s) -> task_id {tid}")
        with open(TASK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"task_id": tid, "batch_size": len(batch), "source": "run_new_products"}) + "\n")
        if tid:
            task_ids.append(tid)
        time.sleep(1)
    return task_ids


def wait_for_import(task_ids, max_wait_s=IMPORT_WAIT_S):
    """-> ({offer_id: status}, {offer_id: errors}) once nothing is pending (or time is up)."""
    from ozon_client import call
    status, errors = {}, {}
    t0 = time.time()
    while True:
        pending = 0
        for tid in task_ids:
            try:
                r = call("/v1/product/import/info", {"task_id": tid})
            except Exception as e:
                log(f"  import/info {tid}: {e}")
                pending += 1
                continue
            for it in (r.get("result") or {}).get("items", []):
                status[it["offer_id"]] = it.get("status")
                if it.get("errors"):
                    errors[it["offer_id"]] = it["errors"]
                if it.get("status") in ("pending", "processing", None):
                    pending += 1
        if not pending or time.time() - t0 > max_wait_s:
            return status, errors
        time.sleep(30)


# --- 9. status -------------------------------------------------------------------------------

def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def telegram(text):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    try:
        import telegram_notify
        telegram_notify.send_telegram(text)
    except Exception as e:
        log(f"Telegram not sent: {e}")


def run(dry_run=False, today=None, test_products=None, test_offers=None):
    today = today or daily_plan.utc_today()
    started = datetime.now()
    report = {"date": today, "dry_run": dry_run, "started_at": started.isoformat(timespec="seconds"),
              "state": "running", "ms_found": {}, "hm_found": {}, "created": {}, "failed": {}}
    if test_products:
        report["test_products"] = test_products
    if test_offers:
        report["test_offers"] = test_offers
    if not dry_run:
        write_json(STATUS_FILE, report)
    remaining, want, open_brands = targets_for_today(today, test_products, test_offers)
    report["plan"] = {"total": remaining["plan_total"], "left": remaining["total"],
                      "created_before": remaining["created_before"], "quota_left": remaining["quota_left"],
                      "allocations": {f"{b}/{l}": n for (b, l), n in want.items()}, "open_brands": sorted(open_brands)}
    if remaining["total"] <= 0:
        log("Nothing planned / left for today -- done.")
        report["state"] = "nothing_planned"
        return report
    ms_labels = labels_for("M&S", want, open_brands)
    hm_labels = labels_for("H&M", want, open_brands)

    ms_found = ms_candidates(ms_labels, want, remaining, report)
    hm_found = {}
    if hm_labels:
        from hm_site import HMBrowser
        try:
            if not wait_for_hm_port():
                raise RuntimeError("H&M Chrome port still busy after 30 min")
            log(f"H&M: categories {hm_labels}")
            with HMBrowser() as hm:
                hm_found = hm_candidates(hm_labels, want, remaining, report, hm)
        except Exception as e:
            # H&M down must not cost the M&S part of the day (the reserve/other categories fill in).
            log(f"! H&M unavailable, continuing with M&S only: {type(e).__name__}: {str(e)[:200]}")
            report["hm_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            hm_found = {}

    log("Picking whole products and building Ozon items...")
    chosen, spares = pick_and_build(ms_found, hm_found, want, open_brands, remaining, dry_run, report)
    if test_products and len(chosen) > test_products:
        # test runs: exactly N products (best priority first); the rest stay spare for failed imports
        chosen.sort(key=lambda e: -_weight(remaining, e[0], e[1]))
        spares = chosen[test_products:] + spares
        chosen = chosen[:test_products]
    items = [it for (_b, _l, its, _s) in chosen for it in its]
    sel = Counter()
    for b, l, its, _s in chosen:
        sel[f"{b}/{l}"] += len(its)
    report["selected"] = dict(sel)
    report["selected_products"] = [{"brand": b, "category": l, "source": s, "offers": [i["offer_id"] for i in its],
                                    "price_usd": its[0]["price"], "name": its[0]["name"]} for b, l, its, s in chosen]
    rs = report["reserve"]
    log(f"Selected {len(items)} offer(s) from {len(chosen)} product(s): {dict(sel)}")
    log(f"Reserve (+15%): planned {rs['planned']}, built {rs['built']}, used for short categories "
        f"{rs['used_for_shortfall']}, {sum(len(e[2]) for e in spares)} spare offer(s) kept for failed imports")
    if dry_run:
        report["state"] = "dry_run_ok"
        return report
    if not items:
        report["state"] = "nothing_found"
        return report

    def send(entries):
        its = [it for (_b, _l, x, _s) in entries for it in x]
        urls = {s for b, _l, _i, s in entries if b == "M&S"}
        save_ms_rows([p for lst in ms_found.values() for p in lst if p["url"] in urls])
        log_new_items(its, today)
        log(f"Uploading {len(its)} new offer(s)...")
        st, er = wait_for_import(upload(its))
        return its, st, er

    items, status, errors = send(chosen)
    ok = [it for it in items if status.get(it["offer_id"]) == "imported"]
    bad = [it for it in items if status.get(it["offer_id"]) != "imported"]
    # Reserve, step 2: replace offers Ozon did not import with spare products (once).
    if bad and spares:
        extra = take_spares(spares, len(bad))
        if extra:
            log(f"{len(bad)} offer(s) not imported -- uploading {sum(len(e[2]) for e in extra)} reserve offer(s) instead")
            x_items, x_status, x_errors = send(extra)
            status.update(x_status)
            errors.update(x_errors)
            items += x_items
            ok += [it for it in x_items if x_status.get(it["offer_id"]) == "imported"]
            bad += [it for it in x_items if x_status.get(it["offer_id"]) != "imported"]
            rs["used_for_failed_imports"] = len(x_items)
            for b, l, its, _s in extra:
                sel[f"{b}/{l}"] += len(its)
            report["selected"] = dict(sel)
    for it in ok:
        k = f"{daily_plan.brand_for_offer_id(it['offer_id'])}/{daily_plan.category_label_for_type(it['type_id'])}"
        report["created"][k] = report["created"].get(k, 0) + 1
    report["failed"] = {it["offer_id"]: {"status": status.get(it["offer_id"]), "errors": errors.get(it["offer_id"])}
                        for it in bad}
    log(f"Ozon import: {len(ok)} imported, {len(bad)} not imported")

    def push_stock(its):
        import update_stocks as us
        # Real run 2026-11-27: the push ran 1 s after a fast import, Ozon had no prices yet, the
        # warehouse routing skipped 499 of 500 offers and none got stock. Wait for the prices first.
        priced = wait_for_prices([it["offer_id"] for it in its])
        updates = [{"offer_id": it["offer_id"], "stock": it["_stock"]} for it in its if it["offer_id"] in priced]
        missing = [it["offer_id"] for it in its if it["offer_id"] not in priced]
        if missing:
            log(f"  ! {len(missing)} offer(s) still have no Ozon price -- stock not pushed for them")
            report.setdefault("stock_not_pushed", []).extend(missing)
        if not updates:
            return
        log(f"Pushing stock for the {len(updates)} new offer(s) only...")
        res = us.push_stock_updates_for(updates)
        report["stock_pushed"] = report.get("stock_pushed", 0) + len(updates)
        if isinstance(res, tuple) and res[1]:
            report["stock_push_failed_rows"] = report.get("stock_push_failed_rows", 0) + res[1]

    def count_rejected(its):
        rej = rejected_after_import([it["offer_id"] for it in its])
        for it in its:
            if it["offer_id"] in rej:
                k = (f"{daily_plan.brand_for_offer_id(it['offer_id'])}/"
                     f"{daily_plan.category_label_for_type(it['type_id'])}")
                report["created"][k] -= 1
                if not report["created"][k]:
                    del report["created"][k]
        report.setdefault("rejected", {}).update(rej)
        return rej

    if ok:
        imported_at = time.time()
        push_stock(ok)
        # Real run 2026-11-27: two SPU_ALREADY_EXISTS refusals came ~1 min after the check ran.
        wait = imported_at + REJECT_CHECK_DELAY_S - time.time()
        if wait > 0:
            log(f"Waiting {int(wait)} s for Ozon moderation before checking for refused cards...")
            time.sleep(wait)
        # Reserve, step 3 (added 2026-11-26, real test 2): an offer can be imported and still be
        # refused at moderation (e.g. SPU_ALREADY_EXISTS_IN_ANOTHER_ACCOUNT). Those never go on
        # sale, so they don't count as created and are replaced with spare products once.
        rej = count_rejected(ok)
        if rej:
            log(f"{len(rej)} imported offer(s) rejected by Ozon after moderation: "
                f"{sorted({c for v in rej.values() for c in v})}")
            # Refused only for the hashtags (real run 2026-11-27: BR_hashtag_brand on brand-free tags,
            # BR_hashtags_symbols_validation): send the same cards again without hashtags.
            tag_only = [it for it in ok if it["offer_id"] in rej and all(c.startswith(HASHTAG_ERROR_PREFIX)
                                                                         for c in rej[it["offer_id"]])]
            if tag_only:
                fixed = resend_without_hashtags(tag_only)
                for it in tag_only:
                    if it["offer_id"] in fixed:
                        del report["rejected"][it["offer_id"]]
                        k = (f"{daily_plan.brand_for_offer_id(it['offer_id'])}/"
                             f"{daily_plan.category_label_for_type(it['type_id'])}")
                        report["created"][k] = report["created"].get(k, 0) + 1
                        rej.pop(it["offer_id"])
                report["hashtags_removed"] = sorted(fixed)
                log(f"  {len(fixed)} of {len(tag_only)} card(s) sent again without hashtags")
            extra = take_spares(spares, len(rej)) if rej else []
            if extra:
                log(f"Uploading {sum(len(e[2]) for e in extra)} reserve offer(s) instead")
                x_items, x_status, _x_errors = send(extra)
                x_ok = [it for it in x_items if x_status.get(it["offer_id"]) == "imported"]
                for it in x_ok:
                    k = (f"{daily_plan.brand_for_offer_id(it['offer_id'])}/"
                         f"{daily_plan.category_label_for_type(it['type_id'])}")
                    report["created"][k] = report["created"].get(k, 0) + 1
                rs["used_for_failed_imports"] += len(x_items)
                if x_ok:
                    push_stock(x_ok)
                    count_rejected(x_ok)
        # Real test 3, 2026-11-27: Ozon could not download some H&M photos for 50 of 100 cards
        # (pics_http_error; a test 4 h earlier had none). Send those cards' photos once more.
        live = [it for it in ok if it["offer_id"] not in (report.get("rejected") or {})]
        retried = retry_failed_images(live)
        if retried:
            report["images_resent"] = retried
            log(f"  photos sent again for {len(retried)} card(s) Ozon could not download them for")
    bad_any = bad or report.get("rejected") or report.get("stock_not_pushed")
    report["state"] = "completed" if not bad_any else "completed_with_errors"
    return report


HASHTAG_ERROR_PREFIX = "BR_hashtag"
PRICE_WAIT_S, PRICE_POLL_S = 900, 30
REJECT_CHECK_DELAY_S = 300


def wait_for_prices(offer_ids, timeout=None, every=None):
    """-> set of offer_ids Ozon already has a sale price for, polling until all have one or the
    timeout runs out. Stock routing ($60 rule) needs that price. Read-only."""
    from warehouse_routing import fetch_final_prices
    timeout = PRICE_WAIT_S if timeout is None else timeout
    every = PRICE_POLL_S if every is None else every
    want, have = set(offer_ids), set()
    deadline = time.time() + timeout
    while True:
        try:
            have = {o for o, p in fetch_final_prices(sorted(want)).items() if p}
        except Exception as e:
            log(f"  price check failed: {e}")
        if have >= want or time.time() >= deadline:
            return have
        log(f"  waiting for Ozon prices: {len(have)}/{len(want)} ready")
        time.sleep(every)


IMAGE_ERROR_CODES = {"pics_http_error", "primary_image_load_failed", "some_image_failed"}


def retry_failed_images(items):
    """Re-imports, unchanged, the cards Ozon flagged with an image download error -- Ozon then
    downloads the photos again (/v1/product/pictures/import is obsolete: "code 9"). -> sorted
    offer_ids re-imported. Errors are logged, never raised."""
    from ozon_client import call
    by_id = {it["offer_id"]: it for it in items if it.get("images")}
    flagged = []
    ids = sorted(by_id)
    for i in range(0, len(ids), 100):
        try:
            cards = call("/v3/product/info/list", {"offer_id": ids[i:i + 100]}).get("items", [])
        except Exception as e:
            log(f"  photo check failed: {e}")
            continue
        flagged += [by_id[c["offer_id"]] for c in cards if c.get("offer_id") in by_id and
                    IMAGE_ERROR_CODES.intersection(e.get("code") for e in c.get("errors") or [])]
    if not flagged:
        return []
    try:
        st, _er = wait_for_import(upload(flagged))
    except Exception as e:
        log(f"  ! photos not re-sent: {str(e)[:160]}")
        return []
    return sorted(o for o, s in st.items() if s == "imported")


def resend_without_hashtags(items):
    """Re-imports the same cards with the hashtags attribute removed. -> set of imported offer_ids."""
    from upload_to_ozon import ATTR_HASHTAGS
    clean = [dict(it, attributes=[a for a in it.get("attributes", []) if a.get("id") != ATTR_HASHTAGS])
             for it in items]
    try:
        st, _er = wait_for_import(upload(clean))
    except Exception as e:
        log(f"  re-sending without hashtags failed: {e}")
        return set()
    return {o for o, s in st.items() if s == "imported"}


def rejected_after_import(offer_ids):
    """-> {offer_id: [error codes]} for new offers Ozon refused to create (card not created and
    an ERROR-level error, e.g. SPU_ALREADY_EXISTS_IN_ANOTHER_ACCOUNT, ML_INCORRECT_VOLUME_WEIGHT).
    Offers still waiting for moderation have no errors and are not counted. Read-only."""
    from ozon_client import call
    out = {}
    for i in range(0, len(offer_ids), 1000):
        try:
            items = call("/v3/product/info/list", {"offer_id": offer_ids[i:i + 1000]}).get("items", [])
        except Exception as e:
            log(f"  card check failed: {e}")
            continue
        for it in items:
            st = it.get("statuses") or {}
            errs = [e for e in it.get("errors") or [] if e.get("level") == "ERROR_LEVEL_ERROR"]
            if errs and not st.get("is_created", True):
                out[it["offer_id"]] = sorted({e.get("code") or "?" for e in errs})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Add new M&S + H&M products to Ozon (daily plan).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="plan date YYYY-MM-DD (UTC) -- for --dry-run checks of a future day")
    ap.add_argument("--test", type=int, metavar="N",
                    help="REAL run of about N whole products under the saved rules, ignoring what was "
                         "already created today (owner's pre-run tests)")
    ap.add_argument("--test-offers", type=int, metavar="N",
                    help="REAL one-off run of N offers under the saved rules, ignoring what was already "
                         "created today (owner's extra test runs)")
    a = ap.parse_args(argv)
    if a.test and a.test_offers:
        ap.error("use either --test or --test-offers")
    if a.date and not a.dry_run:
        ap.error("--date only with --dry-run")
    if os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) < 6 * 3600:
        log(f"{LOCK_FILE} exists -- another run is active. Exiting.")
        return 3
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    report = {"date": a.date or daily_plan.utc_today(), "dry_run": a.dry_run}
    rc = 0
    try:
        report = run(dry_run=a.dry_run, today=a.date, test_products=a.test, test_offers=a.test_offers)
    except Exception as e:
        rc = 1
        report.update(state="failed", error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-3000:])
        log(f"FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if a.dry_run:
        write_json(DRYRUN_FILE, report)
        log(f"Dry run report: {DRYRUN_FILE} (state {report.get('state')})")
    else:
        write_json(STATUS_FILE, report)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in report.items() if k not in ("traceback", "selected_products")},
                               ensure_ascii=False, default=str) + "\n")
        if report.get("state") not in ("nothing_planned",):
            created = sum(report.get("created", {}).values())
            text = (("TEST " if (a.test or a.test_offers) else "") + f"Ozon new products {report['date']}: {report.get('state')}\n"
                    f"created {created} offer(s): {report.get('created') or '-'}\n"
                    f"not imported: {len(report.get('failed') or {})}")
            if report.get("hm_error"):
                text += f"\nH&M skipped: {report['hm_error'][:200]}"
            if report.get("rejected"):
                codes = sorted({c for v in report["rejected"].values() for c in v})
                text += f"\nrejected by Ozon after moderation: {len(report['rejected'])} {codes}"
            if report.get("hashtags_removed"):
                text += f"\nsent again without hashtags: {len(report['hashtags_removed'])}"
            if report.get("stock_not_pushed"):
                text += f"\nNO STOCK pushed (no Ozon price yet): {len(report['stock_not_pushed'])}"
            rs = report.get("reserve") or {}
            if rs:
                text += (f"\nreserve +15%: {rs.get('built', 0)}/{rs.get('planned', 0)} built, used "
                         f"{rs.get('used_for_shortfall', 0)} for short categories, "
                         f"{rs.get('used_for_failed_imports', 0)} for failed imports")
            if report.get("error"):
                text += f"\nerror: {report['error'][:300]}"
            telegram(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
