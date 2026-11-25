"""
New product adding plan: a priority and a "URLs to add" target per brand x
category, for the Ozon Sales app's "New Product Adding" tab.

Pure logic + one local JSON file (new_product_plan.json, gitignored), kept
separate from daily_plan.json. Tk-free, never calls the Ozon API and never
fetches a URL. The numbers are saved and shown only: discovery and the
upload still follow daily_plan.json (offers).

Plan file schema:
    {"version": 1,
     "categories": {"M&S/Pajama": {"priority": "high", "urls_to_add": 40}},
     "updated_at": ISO-8601 string or null}

"Category" is the Ozon product type label from daily_plan.CATEGORY_LABELS.
H&M has no add pipeline yet, so it only gets one read-only row and
set_entry() refuses it.
"""
import copy
import json
import os
import tempfile
from datetime import datetime, timezone

import daily_plan

PLAN_FILE = "new_product_plan.json"
PLAN_VERSION = 1

PRIORITIES = ("very_high", "high", "medium", "low", "very_low")
PRIORITY_LABELS = ("Very high", "High", "Medium", "Low", "Very low")
PRIORITY_LABEL = dict(zip(PRIORITIES, PRIORITY_LABELS))
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
DEFAULT_PRIORITY = "medium"
DEFAULT_URLS_TO_ADD = 0

MS = "M&S"
HM = "H&M"
READ_ONLY_BRANDS = (HM,)
HM_NOTE = "no add pipeline yet / website check pending"
HM_CATEGORY = "All"


def default_plan():
    return {"version": PLAN_VERSION, "categories": {}, "updated_at": None}


def entry_key(brand, label):
    return f"{brand}/{label}"


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _entry_errors(key, entry):
    errors = []
    where = f"categories.{key}"
    if not isinstance(key, str) or "/" not in key:
        return [f"{where}: key must be 'brand/category'"]
    brand, _, label = key.partition("/")
    if not brand or not label:
        return [f"{where}: key must be 'brand/category'"]
    if brand in READ_ONLY_BRANDS:
        errors.append(f"{where}: {brand} has no add pipeline yet (read-only)")
    if not isinstance(entry, dict):
        return errors + [f"{where}: must be an object"]
    if "priority" in entry and entry["priority"] not in PRIORITIES:
        errors.append(f"{where}.priority: must be one of {', '.join(PRIORITIES)} "
                      f"(got {entry['priority']!r})")
    if "urls_to_add" in entry:
        n = entry["urls_to_add"]
        if not _is_int(n) or n < 0:
            errors.append(f"{where}.urls_to_add: must be an int >= 0 (got {n!r})")
    return errors


def validate_plan(plan):
    """Returns a list of human-readable errors; empty means valid."""
    if not isinstance(plan, dict):
        return ["plan: must be an object"]
    cats = plan.get("categories", {})
    if not isinstance(cats, dict):
        return ["categories: must be an object"]
    errors = []
    for key, entry in cats.items():
        errors += _entry_errors(key, entry)
    return errors


def load_plan(path=PLAN_FILE):
    """The saved plan, or an empty plan if the file is missing, unreadable or
    corrupt. Invalid entries are dropped, valid ones kept. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return default_plan()
    if not isinstance(raw, dict) or not isinstance(raw.get("categories", {}), dict):
        return default_plan()
    plan = default_plan()
    for key, entry in (raw.get("categories") or {}).items():
        if not _entry_errors(key, entry):
            plan["categories"][key] = dict(entry)
    updated = raw.get("updated_at")
    plan["updated_at"] = updated if isinstance(updated, str) else None
    return plan


def save_plan(plan, path=PLAN_FILE):
    """Validates, stamps updated_at, then writes atomically (temp file in the
    same directory + os.replace). Raises ValueError if invalid (nothing is
    written). Returns the plan as written."""
    errors = validate_plan(plan)
    if errors:
        raise ValueError("Invalid new product plan: " + "; ".join(errors))
    out = copy.deepcopy(plan)
    out["version"] = PLAN_VERSION
    out.setdefault("categories", {})
    out["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".new_product_plan_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return out


def get_entry(plan, brand, label):
    """{"priority", "urls_to_add"} for brand/label, defaults filled in for a
    missing entry or field."""
    cats = (plan or {}).get("categories") if isinstance(plan, dict) else None
    entry = (cats or {}).get(entry_key(brand, label))
    if not isinstance(entry, dict):
        entry = {}
    priority = entry.get("priority")
    n = entry.get("urls_to_add")
    return {
        "priority": priority if priority in PRIORITIES else DEFAULT_PRIORITY,
        "urls_to_add": n if _is_int(n) and n >= 0 else DEFAULT_URLS_TO_ADD,
    }


def set_entry(plan, brand, label, priority=None, urls_to_add=None):
    """A new plan with brand/label's priority and/or urls_to_add changed (the
    input plan is not modified). Raises ValueError for a read-only brand
    (H&M) or an invalid value."""
    if brand in READ_ONLY_BRANDS:
        raise ValueError(f"{brand} has no add pipeline yet (read-only)")
    if not brand or not label:
        raise ValueError("brand and category are required")
    if priority is not None and priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)} (got {priority!r})")
    if urls_to_add is not None and (not _is_int(urls_to_add) or urls_to_add < 0):
        raise ValueError(f"urls_to_add must be an int >= 0 (got {urls_to_add!r})")
    out = copy.deepcopy(plan) if isinstance(plan, dict) else default_plan()
    if not isinstance(out.get("categories"), dict):
        out["categories"] = {}
    entry = get_entry(out, brand, label)
    if priority is not None:
        entry["priority"] = priority
    if urls_to_add is not None:
        entry["urls_to_add"] = urls_to_add
    out["categories"][entry_key(brand, label)] = entry
    return out


# --- Rows ---------------------------------------------------------------------

def _count(c, key):
    v = c.get(key) if isinstance(c, dict) else None
    return v if _is_int(v) and v >= 0 else 0


def _brand_block(inventory, brand):
    if not isinstance(inventory, dict):
        return {}
    block = (inventory.get("brands") or {}).get(brand)
    return block if isinstance(block, dict) else {}


def build_rows(inventory, plan):
    """One editable row per M&S category (daily_plan.CATEGORY_LABELS order),
    sorted by priority rank with CATEGORY_LABELS order as the tie-break,
    then one read-only H&M row. inventory is the category_inventory.json
    snapshot (or None); missing counts are 0. Pure, never raises on bad
    input shapes."""
    ms_cats = _brand_block(inventory, MS).get("categories")
    if not isinstance(ms_cats, dict):
        ms_cats = {}
    rows = []
    for order, label in enumerate(daily_plan.CATEGORY_LABELS.values()):
        c = ms_cats.get(label) or {}
        live = _count(c, "live_products")
        crawled = _count(c, "crawled_not_live_products")
        discovered = _count(c, "discovered_not_live_products")
        eligible = crawled + discovered
        entry = get_entry(plan, MS, label)
        rows.append({
            "brand": MS,
            "category": label,
            "priority": entry["priority"],
            "priority_label": PRIORITY_LABEL[entry["priority"]],
            "total_urls": live + eligible,
            "eligible_urls": eligible,
            "urls_to_add": entry["urls_to_add"],
            "editable": True,
            "over_eligible": entry["urls_to_add"] > eligible,
            "note": None,
            "_order": order,
        })
    rows.sort(key=lambda r: (PRIORITY_RANK[r["priority"]], r["_order"]))
    for r in rows:
        del r["_order"]
    hm = _brand_block(inventory, HM)
    rows.append({
        "brand": HM,
        "category": HM_CATEGORY,
        "priority": None,
        "priority_label": None,
        "total_urls": None,
        "eligible_urls": None,
        "urls_to_add": None,
        "editable": False,
        "over_eligible": False,
        "note": HM_NOTE,
        "live": hm.get("live"),
        "for_sale": hm.get("for_sale"),
    })
    return rows


def add_all(row):
    """'Add all' button value: every eligible URL (0 if unknown)."""
    n = (row or {}).get("eligible_urls")
    return n if _is_int(n) and n > 0 else 0


def add_half(row):
    """'Add half' button value: half the eligible URLs, rounded down."""
    return add_all(row) // 2
