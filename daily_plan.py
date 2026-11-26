"""
Daily new-offer plan: how many NEW Ozon offers may be created per day, split
by brand and Ozon product type ("category"), with per-date overrides.

Pure logic + one local JSON file (daily_plan.json, gitignored) -- this module
never calls the Ozon API. Callers (upload_to_ozon.py, category_priority.py,
fill_remaining_quota.py, the planner UI) pass in whatever live numbers they
already have, e.g. the daily_create quota remaining.

Plan file schema:
    {"version": 1,
     "default": {"total": 500,
                 "allocations": {"M&S": {"Pajama": 120, "Socks": 80}},
                 "fill_remainder": true},
     "overrides": {"YYYY-MM-DD": {same shape as default}},
     "updated_at": ISO-8601 string or null}

"total" is a ceiling on new offer_ids created that UTC day (all runs combined,
same date stamp as new_items_submitted.json). Allocations reserve part of the
total for specific (brand, category) pairs; with fill_remainder on, whatever
the allocations don't use is filled from the remaining items in priority
order.
"""
import copy
import csv
import json
import os
import re
import tempfile
from collections import Counter
from datetime import date as _date, datetime, timezone

from ozon_mapping import (
    TYPE_ID_BLOUSE, TYPE_ID_DRESS, TYPE_ID_JACKET, TYPE_ID_PAJAMA, TYPE_ID_PANTIES,
    TYPE_ID_PANTIES_SET, TYPE_ID_SKIRT, TYPE_ID_SOCKS, TYPE_ID_SWEATER, TYPE_ID_TANK_TOP,
    TYPE_ID_TOP, TYPE_ID_TROUSERS, TYPE_ID_TSHIRT, extract_article_code_from_offer_id,
    resolve_category_and_type,
)
from warehouse_routing import MS_FAMILY_PREFIXES

PLAN_FILE = "daily_plan.json"
NEW_ITEMS_LOG = "new_items_submitted.json"
PRODUCTS_CSV = "products.csv"

PLAN_VERSION = 1
DEFAULT_TOTAL = 500
MAX_TOTAL = 2000  # Ozon's daily_create limit -- a plan above it is meaningless.
# Owner instruction 2026-11-26: always prepare 15% more products, kept separate, used only to
# complete the day's target (categories that came up short, offers Ozon did not import).
# The reserve never takes the day above its target.
RESERVE_PCT = 0.15


def reserve_for(total):
    """Reserve offers prepared on top of a day's target (ceil 15%, 0 for a 0 day)."""
    import math
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        return 0
    return math.ceil(total * RESERVE_PCT) if total > 0 else 0

CATEGORY_LABELS = {
    TYPE_ID_PANTIES: "Underwear",
    TYPE_ID_PANTIES_SET: "Underwear Set",
    TYPE_ID_TANK_TOP: "Tank Top",
    TYPE_ID_PAJAMA: "Pajama",
    TYPE_ID_TSHIRT: "T-Shirt",
    TYPE_ID_SOCKS: "Socks",
    TYPE_ID_SWEATER: "Sweater",
    TYPE_ID_TROUSERS: "Trousers",
    TYPE_ID_BLOUSE: "Blouse",
    TYPE_ID_SKIRT: "Skirt",
    TYPE_ID_JACKET: "Jacket",
    TYPE_ID_DRESS: "Dress",
    TYPE_ID_TOP: "Top",
}

HM_RE = re.compile(r"^(HM\d?-|\d-HM-)")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- Brand / category ---------------------------------------------------------

def brand_for_offer_id(oid):
    """'M&S', 'H&M' or 'Other' from the offer_id prefix (same rule as
    catalog_status.brand())."""
    oid = oid or ""
    if oid.startswith(MS_FAMILY_PREFIXES):
        return "M&S"
    if HM_RE.match(oid):
        return "H&M"
    return "Other"


def category_label_for_type(type_id):
    return CATEGORY_LABELS.get(type_id)


def category_label_for_name(name):
    """Category label for an M&S product name via the same keyword cascade the
    upload uses, or None if unmapped (skip rather than guess)."""
    # Lazy import: upload_to_ozon is heavy (pandas, translations) and will
    # itself import this module, so a top-level import would be circular.
    from upload_to_ozon import is_multi_pack
    _, type_id = resolve_category_and_type(name, is_multi_pack(name))
    return category_label_for_type(type_id) if type_id is not None else None


# --- Plan file ----------------------------------------------------------------

def default_plan():
    return {
        "version": PLAN_VERSION,
        "default": {"total": DEFAULT_TOTAL, "allocations": {}, "fill_remainder": True},
        "overrides": {},
        "updated_at": None,
    }


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_day(day, where):
    errors = []
    if not isinstance(day, dict):
        return [f"{where}: must be an object"]
    total = day.get("total")
    if not _is_int(total) or not 0 <= total <= MAX_TOTAL:
        errors.append(f"{where}.total: must be an int from 0 to {MAX_TOTAL} (got {total!r})")
    if "fill_remainder" in day and not isinstance(day["fill_remainder"], bool):
        errors.append(f"{where}.fill_remainder: must be true/false")
    allocations = day.get("allocations", {})
    if not isinstance(allocations, dict):
        return errors + [f"{where}.allocations: must be an object"]
    alloc_sum = 0
    for brand, cats in allocations.items():
        if not isinstance(cats, dict):
            errors.append(f"{where}.allocations.{brand}: must be an object")
            continue
        for label, n in cats.items():
            if not _is_int(n) or n < 0:
                errors.append(f"{where}.allocations.{brand}.{label}: must be an int >= 0 (got {n!r})")
            else:
                alloc_sum += n
    if _is_int(total) and alloc_sum > total:
        errors.append(f"{where}: allocations sum to {alloc_sum}, more than total {total}")
    return errors


def validate_plan(plan):
    """Returns a list of human-readable errors; empty means valid."""
    if not isinstance(plan, dict):
        return ["plan: must be an object"]
    errors = _validate_day(plan.get("default"), "default")
    overrides = plan.get("overrides", {})
    if not isinstance(overrides, dict):
        errors.append("overrides: must be an object")
    else:
        for day_key, day in overrides.items():
            if not isinstance(day_key, str) or not _DATE_RE.match(day_key):
                errors.append(f"overrides.{day_key}: key must be YYYY-MM-DD")
                continue
            try:
                _date.fromisoformat(day_key)
            except ValueError:
                errors.append(f"overrides.{day_key}: not a real date")
                continue
            errors += _validate_day(day, f"overrides.{day_key}")
    return errors


def load_plan(path=PLAN_FILE):
    """The saved plan, or the built-in default if the file is missing,
    unreadable, corrupt or invalid. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError):
        return default_plan()
    if validate_plan(plan):
        return default_plan()
    plan.setdefault("version", PLAN_VERSION)
    plan.setdefault("overrides", {})
    plan.setdefault("updated_at", None)
    return plan


def save_plan(plan, path=PLAN_FILE):
    """Validates, stamps updated_at, then writes atomically (temp file in the
    same directory + os.replace). Raises ValueError if invalid. Returns the
    plan as written."""
    errors = validate_plan(plan)
    if errors:
        raise ValueError("Invalid plan: " + "; ".join(errors))
    out = copy.deepcopy(plan)
    out.setdefault("version", PLAN_VERSION)
    out.setdefault("overrides", {})
    out["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".daily_plan_", suffix=".tmp", dir=directory)
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


def _date_key(d):
    if d is None:
        return utc_today()
    if isinstance(d, datetime):
        return d.astimezone(timezone.utc).strftime("%Y-%m-%d") if d.tzinfo else d.strftime("%Y-%m-%d")
    if isinstance(d, _date):
        return d.isoformat()
    return str(d)


def plan_for_date(plan, date):
    """The day's override if there is one, otherwise the default."""
    overrides = plan.get("overrides") or {}
    day = overrides.get(_date_key(date)) or plan.get("default") or default_plan()["default"]
    return {
        "total": day.get("total", DEFAULT_TOTAL),
        "allocations": day.get("allocations") or {},
        "fill_remainder": day.get("fill_remainder", True),
    }


# --- Today's usage ------------------------------------------------------------

def utc_today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _article_labels(products_csv):
    """{ms_article_code: category label or None} from products.csv. Missing or
    locked file (Excel) -> {} so every offer counts toward the total only."""
    labels = {}
    try:
        with open(products_csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("ms_article_code") or "").strip()
                if code and code not in labels:
                    labels[code] = category_label_for_name(row.get("name"))
    except OSError:
        return {}
    return labels


def created_today(path=NEW_ITEMS_LOG, today=None, products_csv=PRODUCTS_CSV):
    """(total, Counter{(brand, label): n}) of new offers already created today
    (UTC). Offers whose category can't be resolved count toward total only."""
    today = _date_key(today)
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        return 0, Counter()
    if not isinstance(log, dict) or log.get("date") != today:
        return 0, Counter()
    offer_ids = [o for o in (log.get("new_offer_ids") or []) if isinstance(o, str)]
    by_cat = Counter()
    # run_new_products.py records each new offer's category in "labels" (H&M offers have no
    # products.csv row); older entries fall back to products.csv by M&S article code.
    logged = log.get("labels") if isinstance(log.get("labels"), dict) else {}
    labels = _article_labels(products_csv) if offer_ids and len(logged) < len(offer_ids) else {}
    for oid in offer_ids:
        code = extract_article_code_from_offer_id(oid)
        label = logged.get(oid) or (labels.get(code) if code else None)
        if label:
            by_cat[(brand_for_offer_id(oid), label)] += 1
    return len(offer_ids), by_cat


def remaining_today(plan, quota_remaining, created_total, created_by_cat, today=None):
    """What may still be created today. quota_remaining=None (quota read
    failed) means only the plan limits it -- the plan cap still applies."""
    day = plan_for_date(plan, today)
    total = max(0, day["total"] - (created_total or 0))
    if quota_remaining is not None:
        total = min(total, max(0, quota_remaining))
    created_by_cat = created_by_cat or {}
    per_category = {}
    for brand, cats in day["allocations"].items():
        for label, alloc in cats.items():
            per_category[(brand, label)] = max(0, alloc - created_by_cat.get((brand, label), 0))
    return {"total": total, "per_category": per_category, "fill_remainder": bool(day["fill_remainder"])}


def apply_cap(new_items, remaining, item_key):
    """Splits the priority-ordered new_items into (accepted, deferred).
    Pass 1 takes items into categories with allocation left (list order);
    pass 2, only if fill_remainder, fills the leftover total from the rest in
    list order. Unmapped items (label None) can only enter via pass 2. Both
    returned lists keep the original relative order."""
    total = max(0, remaining.get("total", 0))
    left = dict(remaining.get("per_category") or {})
    taken = [False] * len(new_items)
    count = 0
    for i, item in enumerate(new_items):
        if count >= total:
            break
        key = item_key(item)
        if key and key[1] is not None and left.get(key, 0) > 0:
            left[key] -= 1
            taken[i] = True
            count += 1
    if remaining.get("fill_remainder"):
        for i in range(len(new_items)):
            if count >= total:
                break
            if not taken[i]:
                taken[i] = True
                count += 1
    accepted = [it for it, t in zip(new_items, taken) if t]
    deferred = [it for it, t in zip(new_items, taken) if not t]
    return accepted, deferred
