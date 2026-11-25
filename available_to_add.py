"""
How many new products are available to add, per brand x category -- so the owner can
choose which categories to start with (then set them in daily_plan.json allocations via
the Ozon Sales app's Prioritization tab).

Read-only toward Ozon and the M&S site. It combines:
  live          offers live on Ozon now                       (category_inventory.json)
  ready now     crawled, in stock, NOT on Ozon and passing every upload check
                (dedup, colour, size, category) -- from
                `upload_to_ozon.py --dry-run --new-only --report`, the exact upload logic
  on site       products found on the M&S site that are not live and not crawled yet,
                with their estimated in-stock offers          (category_inventory --walk)

    python available_to_add.py           uses the last walk in category_inventory.json
    python available_to_add.py --walk    runs `category_inventory.py --walk` first (SLOW,
                                         fetches every M&S category page; read-only)

Writes available_to_add.json (atomic). Never uploads, never writes product_urls.txt,
products.csv, deferred_items.json or daily_plan.json.

Note: "on site" is an estimate before the upload checks -- some of those will be skipped
later as possible colour duplicates or unmappable sizes, as happens to crawled rows.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY_FILE = os.path.join(HERE, "category_inventory.json")
HM_INVENTORY_FILE = os.path.join(HERE, "hm_inventory.json")
OUT_FILE = os.path.join(HERE, "available_to_add.json")
MS, HM = "M&S", "H&M"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run(args, timeout):
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run([sys.executable, *args], cwd=HERE, env=env, timeout=timeout,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ready_now():
    """Per-category new offers that pass every upload check, via the upload's own dry run."""
    fd, tmp = tempfile.mkstemp(prefix="_available_", suffix=".json", dir=HERE)
    os.close(fd)
    try:
        rc, out = run(["upload_to_ozon.py", "--dry-run", "--new-only", "--report", tmp], timeout=1800)
        data = _read_json(tmp)
        if rc != 0 or not data or "by_category" not in data:
            raise RuntimeError(f"upload dry run failed (exit {rc}):\n{out[-1500:]}")
        return data
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def combine(inventory, ready, hm_inventory=None):
    """Pure: rows per brand/category. inventory = category_inventory.json dict (or None),
    ready = write_available_report() dict, hm_inventory = hm_inventory.json (None -> read file)."""
    ms = ((inventory or {}).get("brands") or {}).get(MS) or {}
    cats = ms.get("categories") or {}
    ready_by = {}
    for key, v in (ready.get("by_category") or {}).items():
        brand, _, label = key.partition("/")
        ready_by[(brand, label)] = v
    labels = list(cats) + [lbl for (b, lbl) in ready_by if b == MS and lbl not in cats]
    rows = []
    for lbl in labels:
        c = cats.get(lbl) or {}
        r = ready_by.get((MS, lbl)) or {}
        row = {"brand": MS, "category": lbl,
               "live_offers": c.get("live_offers"),
               "ready_offers": r.get("offers", 0), "ready_articles": r.get("articles", 0),
               "site_products": c.get("discovered_not_live_products", 0),
               "site_offers_est": c.get("discovered_not_live_offers_est", 0)}
        row["total_offers_est"] = row["ready_offers"] + (row["site_offers_est"] or 0)
        rows.append(row)
    rows.sort(key=lambda r: -r["total_offers_est"])
    hm = ((inventory or {}).get("brands") or {}).get(HM) or {}
    walk = (inventory or {}).get("discovery_found") or {}
    hm_inv = _read_json(HM_INVENTORY_FILE) if hm_inventory is None else hm_inventory
    hm_rows = []
    for label, c in ((hm_inv or {}).get("counts") or {}).items():
        hm_rows.append({"brand": HM, "category": label, "live_offers": None, "ready_offers": 0, "ready_articles": 0,
                        "site_products": c.get("products", 0), "site_offers_est": c.get("offers", 0),
                        "total_offers_est": c.get("offers", 0)})
    hm_rows.sort(key=lambda r: -r["total_offers_est"])
    rows += hm_rows
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "walk_date": walk.get("date"),
            "inventory_built": (inventory or {}).get("built"),
            "quota": (inventory or {}).get("quota"),
            "rows": rows,
            "totals": {"ready_offers": sum(r["ready_offers"] for r in rows),
                       "site_offers_est": sum(r["site_offers_est"] or 0 for r in rows),
                       "total_offers_est": sum(r["total_offers_est"] for r in rows)},
            "hm_walked_at": (hm_inv or {}).get("walked_at"),
            "hm": {"live": hm.get("live"), "for_sale": hm.get("for_sale"),
                   "skips": (hm_inv or {}).get("skips"),
                   "note": "from hm_inventory.py" if hm_inv else "run hm_inventory.py for H&M numbers"}}


def write_atomic(data, path=OUT_FILE):
    fd, tmp = tempfile.mkstemp(prefix=".available_", suffix=".tmp", dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def print_table(d):
    def n(v):
        return "-" if v is None else f"{v:,}"
    print(f"\nAvailable to add -- M&S walk {d['walk_date'] or 'NEVER'}, H&M walk {d.get('hm_walked_at') or 'NEVER'}\n")
    head = (f"{'Brand':<6}{'Category':<15}{'Live':>8}{'Ready now':>11}{'On site (products)':>20}"
            f"{'On site (offers~)':>19}{'Total~':>9}")
    print(head)
    print("-" * len(head))
    for brand in (MS, HM):
        rs = [r for r in d["rows"] if r["brand"] == brand]
        for r in rs:
            print(f"{brand:<6}{r['category']:<15}{n(r['live_offers']):>8}{n(r['ready_offers']):>11}"
                  f"{n(r['site_products']):>20}{n(r['site_offers_est']):>19}{n(r['total_offers_est']):>9}")
        print(f"{brand:<6}{'TOTAL':<15}{'':>8}{n(sum(r['ready_offers'] for r in rs)):>11}"
              f"{n(sum(r['site_products'] or 0 for r in rs)):>20}{n(sum(r['site_offers_est'] or 0 for r in rs)):>19}"
              f"{n(sum(r['total_offers_est'] for r in rs)):>9}")
        print("-" * len(head))
    print("Ready now = crawled, in stock, not on Ozon, passes every upload check.")
    print("On site = on the brand's site, not on Ozon; M&S before upload checks, H&M after colour/size/category checks.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--walk", action="store_true", help="run category_inventory.py --walk first (slow)")
    args = ap.parse_args(argv)
    if args.walk:
        print("Walking every M&S category (read-only, slow)...")
        rc, out = run(["category_inventory.py", "--walk"], timeout=4 * 3600)
        if rc != 0:
            print(out[-2000:])
            return 1
    inventory = _read_json(INVENTORY_FILE)
    if not inventory:
        print("category_inventory.json missing -- 'on site' numbers will be 0. Run with --walk.")
    print("Checking crawled products against the upload rules (dry run, nothing is sent)...")
    data = combine(inventory, ready_now())
    write_atomic(data)
    print_table(data)
    print(f"\nSaved to {os.path.basename(OUT_FILE)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
