"""
New-product rules: what the 03:00 new-products job adds, and the live plan the app shows.
Owner design 2026-11-26:
  * one daily number of offers to add (the 15% reserve is extra and hidden -- daily_plan.reserve_for)
  * one min sale price (USD, Ozon price) and one max cost price (TL, M&S / H&M site price) for
    everything; 0 = no limit
  * a priority per brand x category: never / low / medium (default) / high / top / urgent
The job reads the LAST SAVED rules at run time and decides then; during the day the app shows
the products these rules would add right now (cached data), and the plan changes as the rules
change. On the websites the job may see something different (sold out, new prices) -- the
reserve covers that.

How the daily number is split: each category's share is proportional to its priority weight
(never 0, low 1, medium 2, high 4, top 8, urgent 16), never more than it has available (after
the price limits); what a category can't use goes to the others by weight. Inside a category
products are taken whole (all their in-stock sizes), in site order.
"""
import json
import math
import os
import tempfile
from datetime import datetime, timezone

RULES_FILE = "new_product_rules.json"
LEVELS = ("never", "low", "medium", "high", "top", "urgent")
DEFAULT_LEVEL = "medium"
WEIGHTS = {"never": 0, "low": 1, "medium": 2, "high": 4, "top": 8, "urgent": 16}
MAX_DAILY = 2000          # Ozon daily_create limit
TRY_TO_USD = 0.083        # same as upload_to_ozon.TRY_TO_USD_RATE (both brands' first price)
BRANDS = ("M&S", "H&M")


# --- rules file -----------------------------------------------------------------------------

def default_rules():
    return {"version": 1, "daily_offers": 0, "min_sale_usd": 0, "max_cost_try": 0, "priorities": {}}


def cat_key(brand, label):
    return f"{brand}/{label}"


def _num(v, kind):
    if isinstance(v, bool):
        return None
    try:
        x = kind(v)
    except (TypeError, ValueError):
        return None
    return x if x >= 0 else None


def validate(rules):
    """-> list of error strings ([] = ok)."""
    errs = []
    if not isinstance(rules, dict):
        return ["rules must be an object"]
    d = rules.get("daily_offers")
    if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= MAX_DAILY:
        errs.append(f"daily offers must be a whole number 0-{MAX_DAILY}")
    for k in ("min_sale_usd", "max_cost_try"):
        v = rules.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f"{k} must be a number >= 0")
    mn, mx = rules.get("min_sale_usd") or 0, rules.get("max_cost_try") or 0
    if not errs and mn and mx and mx * TRY_TO_USD < mn:
        errs.append(f"no product can pass: max cost {mx:g} TL = ${mx * TRY_TO_USD:.2f} sale, below the "
                    f"min sale price ${mn:g}")
    for k, lvl in (rules.get("priorities") or {}).items():
        if lvl not in LEVELS:
            errs.append(f"{k}: unknown priority {lvl!r}")
    return errs


def load_rules(path=RULES_FILE):
    """Saved rules merged over the defaults; a missing / broken file -> defaults."""
    out = default_rules()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for k in ("daily_offers", "min_sale_usd", "max_cost_try"):
            if k in data:
                out[k] = data[k]
        if isinstance(data.get("priorities"), dict):
            out["priorities"] = {k: v for k, v in data["priorities"].items() if v in LEVELS}
        if data.get("updated_at"):
            out["updated_at"] = data["updated_at"]
    return out if not validate(out) else {**default_rules(), "priorities": out["priorities"]}


def rules_exist(path=RULES_FILE):
    return os.path.exists(path)


def save_rules(rules, path=RULES_FILE):
    """Validated atomic write. -> (saved rules, errors)."""
    errs = validate(rules)
    if errs:
        return None, errs
    out = {"version": 1, "daily_offers": rules["daily_offers"], "min_sale_usd": rules["min_sale_usd"],
           "max_cost_try": rules["max_cost_try"],
           "priorities": {k: v for k, v in sorted((rules.get("priorities") or {}).items()) if v != DEFAULT_LEVEL},
           "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".rules", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return out, []


def priority_of(rules, brand, label):
    return (rules.get("priorities") or {}).get(cat_key(brand, label), DEFAULT_LEVEL)


# --- price limits ---------------------------------------------------------------------------

def sale_usd(cost_try):
    return round(float(cost_try) * TRY_TO_USD, 2)


def price_block(cost_try, rules):
    """None if the product passes the price limits, else the reason."""
    try:
        cost = float(cost_try)
    except (TypeError, ValueError):
        return "no price"
    if cost != cost or cost <= 0:
        return "no price"
    mx, mn = rules.get("max_cost_try") or 0, rules.get("min_sale_usd") or 0
    if mx and cost > mx:
        return "cost above max"
    if mn and sale_usd(cost) < mn:
        return "sale price below min"
    return None


# --- splitting the daily number -------------------------------------------------------------

def allocate(total, available, levels):
    """{cat: offers} -- total split by priority weight, capped by available[cat], leftovers to
    the others by weight (largest remainder). Categories with weight 0 / nothing available get 0."""
    cats = [c for c in available if WEIGHTS.get(levels.get(c, DEFAULT_LEVEL), 0) > 0 and available[c] > 0]
    out = {c: 0 for c in available}
    left = max(0, int(total or 0))
    open_cats = list(cats)
    while left > 0 and open_cats:
        wsum = sum(WEIGHTS[levels.get(c, DEFAULT_LEVEL)] for c in open_cats)
        exact = {c: left * WEIGHTS[levels.get(c, DEFAULT_LEVEL)] / wsum for c in open_cats}
        give = {c: int(v) for c, v in exact.items()}
        rest = left - sum(give.values())
        for c in sorted(open_cats, key=lambda c: (-(exact[c] - give[c]), -WEIGHTS[levels.get(c, DEFAULT_LEVEL)], c))[:rest]:
            give[c] += 1
        used = 0
        for c in open_cats:
            room = available[c] - out[c]
            g = min(give[c], room)
            out[c] += g
            used += g
        left -= used
        open_cats = [c for c in open_cats if out[c] < available[c]]
        if used == 0:
            break
    return out


# --- candidates (cached data) and the plan --------------------------------------------------

def live_ms_tokens(live_offer_ids):
    """Tokens between 'MS-' and the next '-' of live M&S offers: article codes or parent SKUs."""
    out = set()
    for oid in live_offer_ids or []:
        if isinstance(oid, str) and oid.startswith("MS-"):
            out.add(oid[3:].split("-", 1)[0])
    return out


def ms_candidates_from_csv(products_csv, live_tokens, label_for_name):
    """Crawled M&S products not on Ozon, in file order: one per product page (url)."""
    import csv
    by_url = {}
    try:
        with open(products_csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                url = row.get("url")
                if not url:
                    continue
                p = by_url.get(url)
                if p is None:
                    art = (row.get("ms_article_code") or "").strip()
                    psku = (row.get("parent_sku") or "").strip()
                    p = by_url[url] = {"brand": "M&S", "key": url, "url": url, "article": art,
                                       "live": bool((art and art in live_tokens) or (psku and psku in live_tokens)),
                                       "name": row.get("name") or "", "label": label_for_name(row.get("name")),
                                       "cost_try": _num(row.get("price"), float), "offers": 0}
                try:
                    if float(row.get("stock_count") or 0) > 0:
                        p["offers"] += 1
                except ValueError:
                    pass
    except OSError:
        return []
    return [p for p in by_url.values() if not p["live"] and p["label"] and p["offers"] > 0]


def hm_candidates_from_inventory(hm_inventory):
    out = []
    for c in (hm_inventory or {}).get("candidates") or []:
        if c.get("label") and c.get("offers"):
            out.append({"brand": "H&M", "key": c.get("article"), "article": c.get("article"),
                        "url": f"https://www2.hm.com/tr_tr/productpage.{c.get('article')}.html",
                        "name": c.get("title") or "", "label": c["label"], "cost_try": c.get("price_try"),
                        "offers": int(c.get("offers") or 0)})
    return out


def build_plan(rules, candidates):
    """The products these rules would add now. -> {
       "categories": [{brand, label, level, available, products_available, planned}],
       "products": [{..candidate.., planned order}], "total": n, "blocked": {reason: products}}
    Every category in `candidates` appears, also 'never' ones (so the owner can raise them)."""
    blocked = {}
    ok_by_cat = {}
    all_cats = []
    for c in candidates:
        cat = cat_key(c["brand"], c["label"])
        if cat not in ok_by_cat:
            ok_by_cat[cat] = []
            all_cats.append(cat)
        why = price_block(c.get("cost_try"), rules)
        if why:
            blocked[why] = blocked.get(why, 0) + 1
            continue
        ok_by_cat[cat].append(c)
    levels = {cat: priority_of(rules, *cat.split("/", 1)) for cat in all_cats}
    available = {cat: sum(p["offers"] for p in ps) for cat, ps in ok_by_cat.items()}
    alloc = allocate(rules.get("daily_offers") or 0, available, levels)
    order = sorted(all_cats, key=lambda c: (-WEIGHTS[levels[c]], c))
    picked, used = {}, {}
    for cat in order:
        room, got = alloc.get(cat, 0), []
        for p in ok_by_cat[cat]:
            if room <= 0:
                break
            if p["offers"] <= room:
                got.append(p)
                room -= p["offers"]
        picked[cat], used[cat] = got, alloc.get(cat, 0) - room
    # whole products leave gaps -> fill the day's number from the next products, best priority first
    gap = (rules.get("daily_offers") or 0) - sum(used.values())
    for cat in order:
        if gap <= 0 or WEIGHTS[levels[cat]] == 0:
            continue
        for p in ok_by_cat[cat]:
            if gap <= 0:
                break
            if p not in picked[cat] and p["offers"] <= gap:
                picked[cat].append(p)
                used[cat] += p["offers"]
                gap -= p["offers"]
    cats = []
    for cat in order:
        b, l = cat.split("/", 1)
        cats.append({"brand": b, "label": l, "level": levels[cat], "available": available.get(cat, 0),
                     "products_available": len(ok_by_cat[cat]), "planned": used.get(cat, 0),
                     "planned_products": len(picked[cat])})
    products = [dict(p, level=levels[cat]) for cat in order for p in picked[cat]]
    return {"categories": cats, "products": products, "total": sum(used.values()), "blocked": blocked,
            "allocation": {cat: used.get(cat, 0) for cat in order if used.get(cat, 0)}}


def load_candidates(repo=".", products_csv="products.csv"):
    """Cached candidates for the plan preview (M&S products.csv + H&M hm_inventory.json)."""
    import daily_plan
    live = []
    try:
        with open(os.path.join(repo, "catalog_live_offer_ids.json"), encoding="utf-8") as f:
            live = json.load(f).get("offer_ids") or []
    except (OSError, ValueError, AttributeError):
        pass
    hm = None
    try:
        with open(os.path.join(repo, "hm_inventory.json"), encoding="utf-8") as f:
            hm = json.load(f)
    except (OSError, ValueError):
        pass
    ms = ms_candidates_from_csv(os.path.join(repo, products_csv), live_ms_tokens(live),
                                daily_plan.category_label_for_name)
    return ms + hm_candidates_from_inventory(hm)


def plan_day_from_rules(rules, candidates):
    """The daily_plan-shaped day the job uses: {"total", "allocations": {brand: {label: n}},
    "fill_remainder": False}."""
    plan = build_plan(rules, candidates)
    allocs = {}
    for cat, n in plan["allocation"].items():
        b, l = cat.split("/", 1)
        allocs.setdefault(b, {})[l] = n
    return {"total": rules.get("daily_offers") or 0, "allocations": allocs, "fill_remainder": False}, plan
