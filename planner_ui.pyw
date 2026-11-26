"""
Ozon Sales -- tabbed desktop app (one ttk.Notebook window).

Tabs, in order:
  * Overview -- account status from account_snapshot.json (version 2):
    KPI cards (Total SKUs / For sale / Stock up to date (24h) / Price up to
    date (24h), each with the M&S / H&M / Other split and its share of total
    SKUs), Brands (SKUs / for sale + Total), Warehouses (live SKUs and
    avg / min / max live price in USD), a Campaigns card and a 'Sales last
    34 hours' card (both from the 10:00 daily report's overview_metrics,
    warning colour when metrics_built_at is older than 26h), 'data as of'
    (warning colour when older than 26h) and any source that isn't ok.
    Refresh runs account_snapshot.py (offline) and reloads.
  * Daily tasks -- view-only status (DailyTasksTab): scheduled MandsOzon* /
    OzonSales_* tasks, one scrollable card per task (build_task_sections:
    display name via TASK_DISPLAY_NAMES, next run, last run, coloured result
    and six metrics from task_status' task_metrics), the latest
    daily_run card with its per-step list, a stale banner when
    daily_run_status.json isn't today's, and collect errors. Data comes from
    task_status.collect_status() on a worker thread; Refresh (the only
    button) or the 60s auto-refresh, never two collects at once. A
    recheck_stockouts timeout, and an account_snapshot failure or timeout
    (warning-only step), are shown as warnings, not failures.
  * New Product Adding (opens first; class PrioritizationTab, alias
    NewProductAddingTab) -- the URL targets per brand x sub category from
    new_product_plan.json, plus the daily offer plan (daily_plan.json).

Later tasks add or replace tabs through the module-level hook
register_tab(name, builder), usable as a plain call or as a decorator;
builder(app, parent) gets the OzonSalesApp shell (root, tk, ttk, post, px,
label, button) and a tk.Frame already added to the notebook. Tabs must hand
worker-thread results back through app.post (the shell's single queue) and
never touch Tk from a thread or start their own poll loop.

New Product Adding tab:
  * header: Ozon daily_create quota (live read of /v4/product/info/limit,
    'unknown' if it fails), today's plan created/planned
    (new_items_submitted.json), deferred queue size, and per-brand catalog
    numbers from catalog_status.json
  * URL table (scrollable grid, new_product_plan.build_rows over
    category_inventory.json + new_product_plan.json): Brand | Sub category |
    Priority (Very high..Very low; changing it re-sorts the rows) | Total
    URLs | Eligible URLs | URLs to add (int entry, warning colour above
    eligible) | Add all / Add half. The H&M row is read-only ('—', no add
    pipeline yet / website check pending). Save writes new_product_plan.json
    (blocked while new_product_plan.validate_plan reports errors). 'Add a new
    website' opens a form whose Create epic calls new_sources.create_request
    (new_source_requests.json + an EPIC block under '## Todo' in TASKS.md;
    the URL is never fetched). These targets are saved only: the scheduled
    upload still caps by the daily plan (offers).
  * daily-plan editor: Default / Today / Today+1..+7 overrides, total, fill
    remainder, Save (daily_plan.save_plan, blocked while
    daily_plan.validate_plan reports errors), Clear override
  * 'Daily plan allocations (offers)' (collapsed by default): the old
    offer-allocation table (double-click Allocation to edit; H&M read-only)
    with Spread evenly / By pool size
  * actions: Refresh numbers, Full site walk (slow), Preview today's split
    (upload_to_ozon.py --dry-run)

The app is still READ-ONLY toward Ozon: the only Ozon call is the quota read, and the only
scripts it starts are the read-only snapshots (catalog_status.py,
category_inventory.py, the offline account_snapshot.py) plus the upload's --dry-run.
Uploads happen in the scheduled runs, never from here. Every Ozon call and
subprocess runs off the UI thread; results come back through a queue
that the UI thread drains on a root.after poll.

Started from the Desktop shortcut made by install_desktop_shortcut.py. Tkinter + standard library only.
"""
import copy
import json
import os
import queue
import subprocess
import sys
import threading
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# pythonw has no console: sys.stdout/stderr are None, and some repo modules
# call sys.stdout.reconfigure() at import. Point them at a log file first.
if sys.stdout is None or sys.stderr is None:
    try:
        _log = open(HERE / "planner_ui_log.txt", "w", encoding="utf-8", buffering=1)
    except OSError:
        _log = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import daily_plan  # noqa: E402  (pure logic + local JSON; never calls Ozon)
import new_product_plan  # noqa: E402  (pure logic + local JSON; never calls Ozon)
import new_sources  # noqa: E402  (offline: local JSON + TASKS.md; never fetches the URL)
import task_status  # noqa: E402  (read-only: local JSON + a scheduler query, never changes a task)

INVENTORY_FILE = "category_inventory.json"
HM_INVENTORY_FILE = "hm_inventory.json"
CATALOG_STATUS_FILE = "catalog_status.json"
DEFERRED_FILE = "deferred_items.json"
NEW_ITEMS_LOG = "new_items_submitted.json"
PLAN_FILE = daily_plan.PLAN_FILE

MS, HM = "M&S", "H&M"
DEFAULT_KEY = "Default"
OVERRIDE_DAYS = 7
HM_NOTE = "no add pipeline yet"

# The ONLY Ozon endpoint this app may call -- a read. Enforced in ozon_read().
QUOTA_ENDPOINT = "/v4/product/info/limit"
READ_ONLY_ENDPOINTS = frozenset({QUOTA_ENDPOINT})

# Every subprocess the app can start. None of them writes to Ozon:
# catalog_status.py / category_inventory.py are read-only snapshots,
# account_snapshot.py is offline (local JSON only, writes just the local
# account_snapshot.json) and the upload only runs with --dry-run (exits
# before /v3/product/import).
COMMANDS = {
    "catalog_status": ["catalog_status.py"],
    "inventory": ["category_inventory.py"],
    "walk": ["category_inventory.py", "--walk"],
    "preview": ["upload_to_ozon.py", "--dry-run"],
    "account_snapshot": ["account_snapshot.py"],
}
TIMEOUTS = {"catalog_status": 15 * 60, "inventory": 15 * 60, "walk": 8 * 3600, "preview": 30 * 60,
            "account_snapshot": 5 * 60}

BG, CARD, FG, MUTED, LINE = "#1c1c1a", "#262624", "#ecebe7", "#a09f9a", "#3a3a37"
STATUS = {"good": "#3fa66a", "warning": "#d9a43a", "serious": "#e07b39", "critical": "#e5534b", "running": "#4c8bf5"}
FONT = "Segoe UI"
DIM = "#6b6a66"
POLL_MS = 100

COLUMNS = [  # (key, heading, width)
    ("brand", "Brand", 60),
    ("category", "Category", 110),
    ("live", "Live", 70),
    ("crawled_not_live", "Crawled, not live", 115),
    ("deferred", "Deferred", 70),
    ("discovered_est", "Discovered (est. offers)", 150),
    ("created_today", "Created today", 95),
    ("created_7d", "Created 7d", 80),
    ("allocation", "Allocation", 125),
    ("days_of_supply", "Days of supply", 100),
]


# --- Data loading (pure / local files only) -------------------------------------

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def deferred_count(deferred_items):
    return len(deferred_items) if isinstance(deferred_items, list) else 0


def created_today_total(new_items_log, today):
    """Offers created today (UTC) per new_items_submitted.json."""
    if not isinstance(new_items_log, dict) or new_items_log.get("date") != today:
        return 0
    return sum(1 for o in new_items_log.get("new_offer_ids") or [] if isinstance(o, str))


def created_from_history(inventory, today, days=7):
    """{"today": {label: n}, "7d": {label: n}} from category_inventory.json's
    created_history (M&S only -- the only brand with an add pipeline)."""
    out = {"today": {}, "7d": {}}
    hist = (inventory or {}).get("created_history") or []
    t = _date.fromisoformat(today)
    window = {(t - timedelta(days=i)).isoformat() for i in range(days)}
    for entry in hist:
        if not isinstance(entry, dict) or entry.get("date") not in window:
            continue
        for label, n in (entry.get("by_category") or {}).items():
            if not isinstance(n, int):
                continue
            out["7d"][label] = out["7d"].get(label, 0) + n
            if entry["date"] == today:
                out["today"][label] = n
    return out


def merge_created_today(created, by_cat_counter):
    """Replaces created['today'] with a fresher daily_plan.created_today()
    split ({(brand, label): n}) and lifts the 7d numbers to match."""
    out = copy.deepcopy(created)
    old_today = out.get("today") or {}
    new_today = {label: n for (brand, label), n in (by_cat_counter or {}).items() if brand == MS}
    for label in set(old_today) | set(new_today):
        delta = new_today.get(label, 0) - old_today.get(label, 0)
        out["7d"][label] = max(0, out["7d"].get(label, 0) + delta)
    out["today"] = new_today
    return out


def pool_size(row):
    """Offers available to add for a row: crawled + deferred + discovered."""
    return sum(row.get(k) or 0 for k in ("crawled_not_live", "deferred", "discovered_est"))


def days_of_supply(pool, allocation):
    if not allocation:
        return None
    return round(pool / allocation, 1)


def build_rows(inventory, catalog_status, plan_day, created, hm_inventory=None, hm_created=None):
    """Table rows for one plan day. inventory: category_inventory.json dict
    (or None); catalog_status: catalog_status.json dict (or None); plan_day:
    {"total", "allocations", "fill_remainder"}; created: created_from_history()
    shape; hm_inventory: hm_inventory.json (or None); hm_created: {label: n}
    created today for H&M. One editable row per daily_plan category for each
    brand (H&M add pipeline since 2026-11-25: run_new_products.py)."""
    created = created or {}
    allocs = ((plan_day or {}).get("allocations") or {})
    brands = (inventory or {}).get("brands") or {}
    ms_cats = (brands.get(MS) or {}).get("categories") or {}
    rows = []
    for label in daily_plan.CATEGORY_LABELS.values():
        c = ms_cats.get(label) or {}
        row = {
            "brand": MS, "category": label,
            "live": c.get("live_offers", 0),
            "crawled_not_live": c.get("crawled_not_live_offers", 0),
            "deferred": c.get("deferred_offers", 0),
            "discovered_est": c.get("discovered_not_live_offers_est", 0),
            "created_today": (created.get("today") or {}).get(label, 0),
            "created_7d": (created.get("7d") or {}).get(label, 0),
            "allocation": (allocs.get(MS) or {}).get(label, 0),
            "editable": True, "note": "",
        }
        row["pool"] = pool_size(row)
        row["days_of_supply"] = days_of_supply(row["pool"], row["allocation"])
        rows.append(row)

    hm_counts = (hm_inventory or {}).get("counts") or {}
    hm_created = hm_created or {}
    for label in daily_plan.CATEGORY_LABELS.values():
        c = hm_counts.get(label) or {}
        row = {
            "brand": HM, "category": label,
            "live": None,  # Ozon doesn't tell us an H&M offer's category without a full attribute read
            "crawled_not_live": None, "deferred": None,
            "discovered_est": c.get("offers", 0) if hm_inventory else None,
            "created_today": hm_created.get(label, 0), "created_7d": None,
            "allocation": (allocs.get(HM) or {}).get(label, 0),
            "editable": True, "note": "",
        }
        row["pool"] = pool_size(row)
        row["days_of_supply"] = days_of_supply(row["pool"], row["allocation"])
        rows.append(row)
    return rows


def is_editable(row):
    return bool(row and row.get("editable"))


def fmt_cell(key, row):
    if key == "allocation" and not is_editable(row):
        return HM_NOTE
    v = row.get(key)
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def quota_text(quota):
    """(value, sub) for the quota card. quota None -> 'unknown'."""
    if not isinstance(quota, dict) or not isinstance(quota.get("limit"), int) \
            or not isinstance(quota.get("usage"), int):
        return "unknown", "live read failed or not done yet"
    rem = quota["limit"] - quota["usage"]
    return f"{quota['usage']:,} / {quota['limit']:,}", f"{rem:,} left · resets {quota.get('reset_at') or '?'}"


def brand_cards(catalog_status):
    """[(brand, value, sub)] for brands with live offers, from catalog_status.json."""
    out = []
    for brand, x in ((catalog_status or {}).get("brands") or {}).items():
        if not isinstance(x, dict) or not x.get("live"):
            continue
        def n(k):
            v = x.get(k)
            return f"{v:,}" if isinstance(v, int) else "—"
        out.append((brand, f"{n('live')} live",
                    f"{n('for_sale')} for sale\n"
                    f"stock ✓ {n('stock_fresh')} · price ✓ {n('price_fresh')}"))
    return out


# --- Plan editing (pure) -------------------------------------------------------

def day_keys(today):
    """Selector options: Default, then today and the next OVERRIDE_DAYS days."""
    t = _date.fromisoformat(today)
    return [DEFAULT_KEY] + [(t + timedelta(days=i)).isoformat() for i in range(OVERRIDE_DAYS + 1)]


def day_label(key, today):
    if key == DEFAULT_KEY:
        return DEFAULT_KEY
    n = (_date.fromisoformat(key) - _date.fromisoformat(today)).days
    rel = "Today" if n == 0 else f"Today+{n}"
    return f"{rel}  {key}"


def first_planned_day(plan, today):
    """The day the tab opens on: today or the first coming day (within the selector's range)
    that has an override, else Default -- so the numbers the next run will use are on screen."""
    for key in day_keys(today)[1:]:
        if has_override(plan, key):
            return key
    return DEFAULT_KEY


def planned_offers(day, brand, label):
    n = ((day or {}).get("allocations") or {}).get(brand, {}).get(label)
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def has_override(plan, key):
    return key != DEFAULT_KEY and key in (plan.get("overrides") or {})


def get_day(plan, key):
    """Editable copy of the day: the default, the date's override, or (no
    override yet) a copy of the default to start from."""
    if key != DEFAULT_KEY and key in (plan.get("overrides") or {}):
        src = plan["overrides"][key]
    else:
        src = plan.get("default") or daily_plan.default_plan()["default"]
    return {
        "total": src.get("total", daily_plan.DEFAULT_TOTAL),
        "allocations": copy.deepcopy(src.get("allocations") or {}),
        "fill_remainder": src.get("fill_remainder", True),
    }


def _clean_day(day):
    allocs = {}
    for brand, cats in (day.get("allocations") or {}).items():
        kept = {lbl: n for lbl, n in cats.items() if n}
        if kept:
            allocs[brand] = kept
    return {"total": day["total"], "allocations": allocs, "fill_remainder": bool(day["fill_remainder"])}


def set_day(plan, key, day):
    """New plan with `day` stored as the default or as the date's override."""
    out = copy.deepcopy(plan)
    out.setdefault("overrides", {})
    if key == DEFAULT_KEY:
        out["default"] = _clean_day(day)
    else:
        out["overrides"][key] = _clean_day(day)
    return out


def clear_override(plan, key):
    out = copy.deepcopy(plan)
    if key != DEFAULT_KEY:
        (out.get("overrides") or {}).pop(key, None)
    return out


def set_allocation(day, brand, label, n):
    """New day with allocation[brand][label] = n (M&S or H&M)."""
    if brand not in (MS, HM):
        raise ValueError(f"unknown brand {brand!r}")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError(f"allocation must be an int >= 0 (got {n!r})")
    out = copy.deepcopy(day)
    out.setdefault("allocations", {}).setdefault(brand, {})[label] = n
    return out


def allocation_sum(day):
    return sum(n for cats in (day.get("allocations") or {}).values()
               for n in cats.values() if isinstance(n, int))


def spread_evenly(total, labels):
    """{label: n} splitting total as evenly as possible (remainder to the first)."""
    labels = list(labels)
    if not labels or total <= 0:
        return {lbl: 0 for lbl in labels}
    base, extra = divmod(total, len(labels))
    return {lbl: base + (1 if i < extra else 0) for i, lbl in enumerate(labels)}


def by_pool_size(total, pools):
    """{label: n} proportional to pool size (largest remainder), never more
    than a label's pool. Labels with an empty pool get 0."""
    pools = {lbl: max(0, p or 0) for lbl, p in pools.items()}
    pool_sum = sum(pools.values())
    if total <= 0 or pool_sum == 0:
        return {lbl: 0 for lbl in pools}
    if pool_sum <= total:
        return dict(pools)
    exact = {lbl: total * p / pool_sum for lbl, p in pools.items()}
    out = {lbl: int(v) for lbl, v in exact.items()}
    left = total - sum(out.values())
    for lbl in sorted(exact, key=lambda k: (-(exact[k] - out[k]), k))[:left]:
        out[lbl] += 1
    return out


def apply_helper(day, allocations):
    """New day whose allocations are replaced by `allocations`: {label: n} (M&S, the old
    shape) or {(brand, label): n} (both brands)."""
    out = copy.deepcopy(day)
    allocs = out.setdefault("allocations", {})
    if all(isinstance(k, tuple) for k in allocations):
        for brand in {b for b, _ in allocations}:
            allocs[brand] = {}
        for (brand, label), n in allocations.items():
            allocs[brand][label] = n
    else:
        allocs[MS] = dict(allocations)
    return out


def plan_errors(plan):
    return daily_plan.validate_plan(plan)


def save_plan_from_ui(plan, path=PLAN_FILE):
    """Saves via daily_plan.save_plan. Returns (saved_plan, []) or
    (None, errors) -- nothing is written when the plan is invalid."""
    errors = plan_errors(plan)
    if errors:
        return None, errors
    return daily_plan.save_plan(plan, path), []


# --- Quick plan: "how many tomorrow" per brand x category (pure) ----------------------
# Owner 2026-11-26: "i still dont get how to plan tomorrows new products make it easier".
# One box per category, the total is the sum, one Save writes the next run's day.

def quick_plan_rows(build_rows_out, day):
    """[{brand, category, available, planned}] for every category that has products to add
    or a number already planned; brand order M&S, H&M, then most available first."""
    out = []
    for r in build_rows_out or []:
        avail = r.get("pool") or 0
        planned = planned_offers(day, r["brand"], r["category"])
        if avail > 0 or planned > 0:
            out.append({"brand": r["brand"], "category": r["category"], "available": avail, "planned": planned})
    out.sort(key=lambda r: (0 if r["brand"] == MS else 1, -r["available"], r["category"]))
    return out


def parse_quick_value(text):
    """'' -> 0, '40' -> 40; None for anything that is not a whole number >= 0."""
    t = (text or "").strip()
    if not t:
        return 0
    if not t.isdigit():
        return None
    return int(t)


def quick_plan_day(values):
    """{(brand, label): n} -> (day dict, errors). Total = the sum; no fill-remainder."""
    errors, allocs = [], {}
    for (brand, label), n in values.items():
        if n is None:
            errors.append(f"{brand} {label}: not a whole number")
        elif n > 0:
            allocs.setdefault(brand, {})[label] = n
    total = sum(n for cats in allocs.values() for n in cats.values())
    if total > daily_plan.MAX_TOTAL:
        errors.append(f"total {total} is above Ozon's daily limit {daily_plan.MAX_TOTAL}")
    return {"total": total, "allocations": allocs, "fill_remainder": False}, errors


def last_planned_day(plan, before):
    """Most recent override (date < before) with a total > 0, as a day dict, or None."""
    overrides = plan.get("overrides") or {}
    for key in sorted((k for k in overrides if k < before), reverse=True):
        if (overrides[key] or {}).get("total"):
            return get_day(plan, key), key
    return None, None


def quick_plan_summary(values, quota):
    """One line: total, reserve, quota check."""
    n = sum(v for v in values.values() if isinstance(v, int))
    parts = [f"Total {n:,} offers"]
    if n:
        parts.append(f"+{daily_plan.reserve_for(n):,} reserve (15%, only used to reach the total)")
    if quota:
        left = quota["limit"] - quota["usage"]
        parts.append(f"Ozon quota now {left:,} / {quota['limit']:,} (resets before the run)")
    return "  ·  ".join(parts)


# --- New Product Adding table (pure) -------------------------------------------------

NP_PLAN_FILE = new_product_plan.PLAN_FILE
NP_COLUMNS = (("brand", "Brand"), ("category", "Sub category"), ("priority", "Priority"),
              ("total_urls", "Total URLs"), ("eligible_urls", "Eligible URLs"),
              ("planned", "Planned offers"), ("urls_to_add", "URLs to add"), ("actions", ""))
NP_CAPTION = ("Eligible = products on the M&S / H&M site that are not on Ozon yet (last site walk). URL targets "
              "and priorities are saved as notes; what the 03:00 job actually adds is the daily plan below "
              "(offers per brand x category).")
ALLOC_SECTION_TITLE = "Daily plan allocations (offers) -- what the 03:00 new-products job adds"
URLS_MAX_DIGITS = 7


def np_fmt(value):
    """'—' for a missing count, else a thousands-separated int."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return DASH


def urls_entry_ok(proposed):
    """Tk validatecommand for 'URLs to add': digits only (or empty while typing)."""
    return proposed == "" or (proposed.isdigit() and len(proposed) <= URLS_MAX_DIGITS)


def parse_urls_to_add(text):
    """int >= 0 from the entry text; ValueError for anything else (incl. empty)."""
    t = (text or "").strip()
    if not t.isdigit() or len(t) > URLS_MAX_DIGITS:
        raise ValueError(f"URLs to add must be a whole number >= 0 (got {text!r})")
    return int(t)


def priority_from_label(label):
    """'Very high' -> 'very_high'; None for an unknown label."""
    return dict(zip(new_product_plan.PRIORITY_LABELS, new_product_plan.PRIORITIES)).get(label)


def np_is_dirty(plan, saved):
    """True when any brand/category's priority or urls_to_add differs from
    the saved plan (missing entries count as the defaults)."""
    def cats(p):
        c = p.get("categories") if isinstance(p, dict) else None
        return c if isinstance(c, dict) else {}
    for key in set(cats(plan)) | set(cats(saved)):
        brand, _, label = str(key).partition("/")
        if new_product_plan.get_entry(plan, brand, label) != new_product_plan.get_entry(saved, brand, label):
            return True
    return False


def np_save_state(plan, saved):
    """(errors, note) for the URL table's Save button and its note."""
    errors = new_product_plan.validate_plan(plan)
    if errors:
        return errors, "Can't save: " + "; ".join(errors)
    if np_is_dirty(plan, saved):
        return errors, "Unsaved changes."
    return errors, ""


# --- Background work (never on the UI thread) -------------------------------------

def ozon_read(path, payload=None):
    """The app's only door to Ozon, limited to READ_ONLY_ENDPOINTS."""
    if path not in READ_ONLY_ENDPOINTS:
        raise PermissionError(f"planner_ui may not call {path}")
    from ozon_client import call
    return call(path, payload or {}, retries=1)


def read_quota():
    """{limit, usage, reset_at} or None (network down, no credentials...)."""
    try:
        dc = (ozon_read(QUOTA_ENDPOINT) or {}).get("daily_create") or {}
        if isinstance(dc.get("limit"), int) and isinstance(dc.get("usage"), int):
            return {"limit": dc["limit"], "usage": dc["usage"], "reset_at": dc.get("reset_at")}
    except Exception as e:
        print(f"Quota read failed: {e}")
    return None


def python_exe():
    """python.exe next to the running interpreter (pythonw.exe has no stdout)."""
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")
    return str(cand if cand.exists() else exe)


def command_argv(action):
    return [python_exe(), "-u"] + list(COMMANDS[action])


def run_script(action, cwd=HERE):
    """(returncode, output) for one COMMANDS entry, hidden window."""
    argv = command_argv(action)
    if not (Path(cwd) / COMMANDS[action][0]).exists():
        return None, f"{COMMANDS[action][0]} not found in {cwd} -- skipped."
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUTS[action], env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return None, out + f"\nTimed out after {TIMEOUTS[action]}s."
    except Exception as e:
        return None, f"Could not start {' '.join(argv)}: {e}"


def tail(text, n=3):
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


# --- Daily tasks (pure) -----------------------------------------------------------
# Turns a task_status.collect_status() dict into rows + STATUS colour keys.
# No tkinter, no schtasks calls; every function takes any input without raising.

TASK_STALE_HOURS = 26
TASK_MISSED_GRACE = timedelta(minutes=10)
TASK_TIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)
CODE_RUNNING, CODE_DISABLED, CODE_NEVER_RUN = 0x41301, 0x41302, 0x41303
RUNNING_WORDS = ("running", "çalışıyor")
DISABLED_WORDS = ("disabled", "devre dışı", "devre dişi")
NORMAL_TIMEOUT_STEPS = ("recheck_stockouts",)  # CLAUDE.md: under-provisioned, normal
WARNING_ONLY_STEPS = ("account_snapshot",)  # daily_run: never fails the run
DASH = "—"


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    """str(value).strip() for real values, '' for None or anything str() chokes on."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _fold(value):
    return _text(value).casefold()


def _code(task):
    code = _as_dict(task).get("last_result_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def parse_task_time(s):
    """Best-effort parse of a schtasks (possibly localized) time -> datetime or None."""
    if not isinstance(s, str):
        return None
    s = " ".join(s.split())
    if not s:
        return None
    for fmt in TASK_TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def task_is_stale(task, now=None):
    """True when the trigger was missed (next_run > 10 min in the past) or, if
    next_run is missing/unparseable, the last run is older than TASK_STALE_HOURS.
    An unparseable last_run is never stale."""
    try:
        task = _as_dict(task)
        now = now if isinstance(now, datetime) else datetime.now()
        last = parse_task_time(task.get("last_run"))
        if last is None:
            return False
        nxt = parse_task_time(task.get("next_run"))
        if nxt is not None:
            return nxt < now - TASK_MISSED_GRACE
        return now - last > timedelta(hours=TASK_STALE_HOURS)
    except Exception:
        return False


def _task_running(task):
    status = _fold(task.get("status"))
    return status in RUNNING_WORDS or _code(task) == CODE_RUNNING


def _task_disabled(task):
    if _code(task) == CODE_DISABLED:
        return True
    text = _fold(task.get("state")) + " " + _fold(task.get("status"))
    return any(w in text for w in DISABLED_WORDS)


def task_result_level(task, now=None):
    """-> STATUS key: running / warning (never run, stale, disabled, unknown) / good / critical."""
    try:
        task = _as_dict(task)
        if _task_running(task):
            return "running"
        if task.get("last_run") is None or _code(task) == CODE_NEVER_RUN:
            return "warning"
        if task_is_stale(task, now) or _task_disabled(task):
            return "warning"
        if task.get("ok") is True:
            return "good"
        if task.get("ok") is False:
            return "critical"
    except Exception:
        pass
    return "warning"


# Display-only names for the Daily tasks tab (matched case-insensitively on the
# task's short name). The Task Scheduler tasks themselves are NOT renamed.
TASK_DISPLAY_NAMES = {
    "OzonSales_DailyStockUpdate": "MS stock update",
    "OzonSales_DailyHMStockUpdate": "H&M stock update",
    "OzonSales_DailyPriceUpdate": "Price update",
    "OzonSales_DailyNewProducts": "New products upload",
}
_TASK_DISPLAY_FOLDED = {k.casefold(): v for k, v in TASK_DISPLAY_NAMES.items()}


def task_display_name(name):
    """Friendly name for a scheduled task (short name or full path); unknown
    names come back unchanged."""
    raw = _text(name)
    if not raw:
        return raw
    short = raw.replace("/", "\\").rstrip("\\").split("\\")[-1].strip()
    return _TASK_DISPLAY_FOLDED.get(short.casefold(), raw)


def build_task_rows(status, now=None):
    """One row per status['tasks'] entry, sorted by next_run (parsed) then name."""
    try:
        tasks = _as_dict(status).get("tasks")
        tasks = tasks if isinstance(tasks, (list, tuple)) else []
        rows = []
        for t in tasks:
            t = _as_dict(t)
            level = task_result_level(t, now)
            result = _text(t.get("last_result_text")) or "unknown"
            if task_is_stale(t, now):
                result += " (stale)"
            raw_name = _text(t.get("name"))
            rows.append({
                "name": task_display_name(raw_name) or "?",
                "task_name": raw_name,
                "next_run": _text(t.get("next_run")) or DASH,
                "last_run": _text(t.get("last_run")) or "never",
                "result": result,
                "status": _text(t.get("status")) or _text(t.get("state")),
                "level": level,
                "tag": level,
                "_next": parse_task_time(t.get("next_run")),
            })
        rows.sort(key=lambda r: (0, r["_next"], r["name"]) if r["_next"] else (1, datetime.min, r["name"]))
        for r in rows:
            r.pop("_next", None)
        return rows
    except Exception:
        return []


# Per-task section metrics: (label, task_status metric field), fixed order.
TASK_SECTION_METRICS = (
    ("URLs checked", "urls_checked"),
    ("Prices checked", "prices_checked"),
    ("SKU prices updated", "sku_price_updated"),
    ("Campaign limit prices updated", "campaign_limit_updated"),
    ("SKU stock checked", "sku_stock_checked"),
    ("SKU stock updated", "sku_stock_updated"),
)
# task_metrics key -> the fields that apply to it (everything else shows '—').
TASK_METRIC_APPLIES = {
    "ms_stock": ("urls_checked", "sku_stock_checked", "sku_stock_updated"),
    "hm_stock": ("urls_checked", "sku_stock_checked", "sku_stock_updated"),
    "price": ("urls_checked", "prices_checked", "sku_price_updated", "campaign_limit_updated"),
}


def _metric_date(key, entry):
    """Date ('YYYY-MM-DD') the counts are from; for 'price' the older of the
    refresh and update dates. None when unknown."""
    names = ("refresh_date", "update_date") if key == "price" else ("date",)
    dates = [d[:10] for d in (_text(entry.get(n)) for n in names) if d]
    return min(dates) if dates else None


def _metric_key(name):
    try:
        return task_status.metric_key_for_task(name)
    except Exception:
        return None


NEW_PRODUCTS_TASK = "ozonsales_dailynewproducts"
NEW_PRODUCTS_STATUS_FILE = "new_products_status.json"


def next_new_products_day(last_status, utc_now=None):
    """UTC plan date the next 03:00 Moscow (00:00 UTC) run will use: today while today's run
    hasn't happened yet (00:00-01:00 UTC, no status for today), otherwise tomorrow."""
    utc_now = utc_now or datetime.now(timezone.utc)
    today = utc_now.date()
    ran_today = _text(_as_dict(last_status).get("date")) == today.isoformat()
    if utc_now.hour < 1 and not ran_today:
        return today.isoformat()
    return (today + timedelta(days=1)).isoformat()


def collect_new_products_panel(utc_now=None, repo=HERE):
    """Worker thread: what the new-products card shows. Local files + one read-only quota call."""
    last = load_json(Path(repo) / NEW_PRODUCTS_STATUS_FILE, {}) or {}
    plan = daily_plan.load_plan(str(Path(repo) / PLAN_FILE))
    day_key = next_new_products_day(last, utc_now)
    day = daily_plan.plan_for_date(plan, day_key)
    return {"last": last, "next_day": day_key, "next_plan": day,
            "reserve": daily_plan.reserve_for(day.get("total")), "quota": read_quota()}


def _plan_split_text(day):
    parts = [f"{b} {lbl} {n}" for b, cats in ((day or {}).get("allocations") or {}).items()
             for lbl, n in (cats or {}).items() if n]
    return " · ".join(parts)


def new_products_metrics(panel):
    """-> ([(label, value, level)] x6, note, note_level) for the new-products card. Pure."""
    p = _as_dict(panel)
    day = _as_dict(p.get("next_plan"))
    total = day.get("total") or 0
    nd = _text(p.get("next_day")) or "?"
    q = p.get("quota") if isinstance(p.get("quota"), dict) else None
    last = _as_dict(p.get("last"))
    created = sum(v for v in _as_dict(last.get("created")).values() if isinstance(v, int))
    last_plan = _as_dict(last.get("plan")).get("total")
    rs = _as_dict(last.get("reserve"))
    metrics = [
        (f"Next run plan ({nd})", f"{total} offers" if total else "0 -- nothing planned",
         "good" if total else "warning"),
        ("Reserve +15% (separate)", f"{p.get('reserve') or 0} offers" if total else DASH,
         "good" if total else "muted"),
        ("Today's Ozon quota left",
         f"{q['limit'] - q['usage']} / {q['limit']}" if q else "quota read failed",
         ("good" if q["limit"] - q["usage"] >= total else "warning") if q else "warning"),
        (f"Last run ({_text(last.get('date')) or '?'})",
         f"{_text(last.get('state')) or 'never'} · created {created}"
         + (f" / {last_plan}" if last_plan is not None else ""),
         "good" if _text(last.get("state")) == "completed" else ("muted" if not last else "warning")),
        ("Not imported", fmt_num(len(_as_dict(last.get("failed")))) if last else DASH,
         "good" if last and not last.get("failed") else ("muted" if not last else "warning")),
        ("Reserve used (short / failed)",
         f"{rs.get('used_for_shortfall', 0)} / {rs.get('used_for_failed_imports', 0)}" if rs else DASH,
         "good" if rs else "muted"),
    ]
    split = _plan_split_text(day)
    note = f"{nd}: {split}" if split else (f"{nd}: no categories set -- set the plan in New Product Adding"
                                           if not total else None)
    if last.get("error"):
        return metrics, f"last run error: {_text(last['error'])[:200]}", "critical"
    return metrics, note, ("muted" if split else "warning")


def build_task_sections(status, now=None):
    """One section per status['tasks'] entry (build_task_rows order):
    {title, task_name, next_run, last_run, result, level,
     metrics: [(label, value_text, level)] x6, note}. Pure, never raises."""
    try:
        st = _as_dict(status)
        all_metrics = _as_dict(st.get("task_metrics"))
        today = _text(st.get("today"))[:10]
        sections = []
        for row in build_task_rows(st, now):
            key = _metric_key(row.get("task_name"))
            entry = _as_dict(all_metrics.get(key)) if key in TASK_METRIC_APPLIES else {}
            applies = TASK_METRIC_APPLIES.get(key, ()) if entry else ()
            metrics = []
            for label, field in TASK_SECTION_METRICS:
                value = entry.get(field) if field in applies else None
                text = fmt_num(value)
                metrics.append((label, text, "muted" if text == DASH else "good"))
            note, note_level = None, None
            if _fold(row.get("task_name")).replace("/", "\\").split("\\")[-1] == NEW_PRODUCTS_TASK:
                metrics, note, note_level = new_products_metrics(st.get("new_products"))
            elif entry:
                when = _metric_date(key, entry)
                if when and today and when != today:
                    note = f"counts from {when} (latest run not recorded)"
                    note_level = "warning"
            sections.append({
                "title": row.get("name") or "?",
                "task_name": row.get("task_name") or "",
                "next_run": row.get("next_run") or DASH,
                "last_run": row.get("last_run") or "never",
                "result": row.get("result") or "unknown",
                "level": row.get("level") or "warning",
                "metrics": metrics,
                "note": note,
                "note_level": note_level,
            })
        return sections
    except Exception:
        return []


def _step_is_normal_timeout(step):
    text = _fold(step.get("script")) + " " + _fold(step.get("name"))
    return any(k in text for k in NORMAL_TIMEOUT_STEPS)


def _step_is_warning_only(step):
    text = _fold(step.get("script")) + " " + _fold(step.get("name"))
    return any(k in text for k in WARNING_ONLY_STEPS)


def step_level(step):
    """daily_run step record -> STATUS key. A recheck_stockouts timeout, and any
    failure or timeout of a warning-only step (account_snapshot), is a warning."""
    try:
        step = _as_dict(step)
        status = _fold(step.get("status"))
        if status == "ok":
            return "good"
        if _step_is_warning_only(step) and (
                status in ("timeout", "timed out", "failed") or step.get("ok") is False
                or any(w in status for w in ("fail", "error", "crash"))):
            return "warning"
        if status in ("timeout", "timed out"):
            return "warning" if _step_is_normal_timeout(step) else "critical"
        if (status == "failed" or step.get("ok") is False
                or any(w in status for w in ("fail", "error", "crash"))):
            return "critical"
    except Exception:
        pass
    return "warning"


def fmt_seconds(value):
    """12m 03s / 1h 02m 03s / '—'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return DASH
    try:
        total = int(round(value))
    except (OverflowError, ValueError):
        return DASH
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _step_result_text(step, level):
    status = _fold(step.get("status"))
    if status == "ok":
        return "ok"
    if status in ("timeout", "timed out"):
        return "TIMED OUT (normal for this step)" if level == "warning" else "TIMED OUT"
    if level == "critical":
        return "FAILED"
    return _text(step.get("status")) or "unknown"


def build_step_rows(daily_run):
    try:
        steps = _as_dict(daily_run).get("steps")
        steps = steps if isinstance(steps, (list, tuple)) else []
        rows = []
        for s in steps:
            s = _as_dict(s)
            level = step_level(s)
            rows.append({
                "name": _text(s.get("name")) or _text(s.get("script")) or "?",
                "script": _text(s.get("script")),
                "result": _step_result_text(s, level),
                "seconds": fmt_seconds(s.get("seconds")),
                "level": level,
            })
        return rows
    except Exception:
        return []


RUN_STATE_LEVELS = {"completed": "good", "failed": "critical", "blocked": "critical", "running": "running"}


def attention_items(daily_run):
    try:
        items = _as_dict(daily_run).get("attention")
        if not isinstance(items, (list, tuple)):
            return []
        return [t for t in (_text(i) for i in items) if t]
    except Exception:
        return []


def build_run_summary(daily_run):
    """-> [(label, value, level)] for the latest-run panel."""
    try:
        dr = _as_dict(daily_run)
        state = _text(dr.get("state")) or "unknown"
        running = state == "running"
        created, plan_total = dr.get("created_today"), dr.get("plan_total")
        plan_level = "warning" if created is None or plan_total is None else "good"
        plan_text = f"{_text(created) or '?'} / {_text(plan_total) or '?'}"
        content = _text(dr.get("content_summary"))
        upload = _text(dr.get("upload_totals"))
        n_att = len(attention_items(dr))
        return [
            ("State", state, RUN_STATE_LEVELS.get(state, "warning")),
            ("Date", _text(dr.get("date")) or DASH, "muted"),
            ("Started", _text(dr.get("started_at")) or DASH, "muted"),
            ("Finished", DASH if running else (_text(dr.get("finished_at")) or DASH), "muted"),
            ("Plan created/total", plan_text, plan_level),
            ("Content", content or "content status unknown", "good" if content else "warning"),
            ("Upload totals", upload or "upload status unknown", "good" if upload else "warning"),
            ("Attention", str(n_att), "warning" if n_att > 0 else "good"),
        ]
    except Exception:
        return [("State", "unknown", "warning")]


def stale_banner(status):
    """Banner text when the latest daily_run_status.json isn't today's, else None."""
    try:
        st = _as_dict(status)
        dr = _as_dict(st.get("daily_run"))
        if dr.get("is_today") or not dr.get("stale"):
            return None
        today = _text(st.get("today")) or "?"
        date = _text(dr.get("date"))
        if date:
            return f"Latest daily run is not from today (file date {date}, today {today})"
        return f"Latest daily run is not from today (file has no date, today {today})"
    except Exception:
        return None


def error_lines(status):
    """Deduplicated str() of status['errors'] + tasks_error, in order."""
    try:
        st = _as_dict(status)
        errors = st.get("errors")
        errors = list(errors) if isinstance(errors, (list, tuple)) else ([errors] if errors else [])
        out = []
        for e in errors + [st.get("tasks_error")]:
            t = _text(e)
            if t and t not in out:
                out.append(t)
        return out
    except Exception:
        return []


def collected_text(status):
    """'collected HH:MM:SS' from status['collected_at'] (ISO), or 'collected —'."""
    try:
        raw = _text(_as_dict(status).get("collected_at"))
        return f"collected {datetime.fromisoformat(raw):%H:%M:%S}" if raw else f"collected {DASH}"
    except Exception:
        return f"collected {DASH}"


def collect_daily_status():
    """task_status.collect_status(), or a fallback dict with an errors entry if it
    raises (or returns something that isn't a dict). Runs on a worker thread -- no Tk."""
    try:
        result = task_status.collect_status()
        if isinstance(result, dict):
            try:
                result["new_products"] = collect_new_products_panel()
            except Exception as e:
                result.setdefault("errors", []).append(f"new products panel: {type(e).__name__}: {e}")
            return result
        error = f"collect_status returned {type(result).__name__}, not a dict"
    except Exception as e:
        error = f"collect_status failed: {type(e).__name__}: {e}"
    now = datetime.now()
    return {"collected_at": now.isoformat(timespec="seconds"), "today": now.strftime("%Y-%m-%d"),
            "tasks": [], "daily_run": {"state": "unknown"}, "last_runs": [], "errors": [error]}


def level_color(level):
    try:
        return STATUS.get(level, MUTED)
    except TypeError:  # unhashable garbage
        return MUTED


# --- Overview data shaping (pure; no Tk, never raises) ----------------------------

ACCOUNT_SNAPSHOT_FILE = "account_snapshot.json"
OVERVIEW_BRANDS = ("M&S", "H&M", "Other")
# Must match catalog_status.FRESH_HOURS (a test checks it). Not imported:
# catalog_status pulls in ozon_client at import and reconfigures stdout.
SNAPSHOT_STALE_HOURS = 26
NO_SNAPSHOT_TEXT = "No snapshot yet: click Refresh"
CORRUPT_SNAPSHOT_TEXT = "Snapshot unreadable (account_snapshot.json is corrupt): click Refresh"
NO_METRICS_TEXT = "No sales/campaign data yet: the 10:00 daily report writes it"
WAREHOUSE_FIXED = ("Small items", "Regular", "Unassigned")
WAREHOUSE_UNKNOWN_NOTE = "Unknown: no live offer_id list"
KPI_SPEC = (("offers", "Total SKUs"), ("for_sale", "For sale"),
            ("stock_up_to_date", "Stock up to date (24h)"),
            ("price_up_to_date", "Price up to date (24h)"))
CAMPAIGNS_TITLE = "Campaigns"
SALES_TITLE = "Sales last 34 hours"


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


def _dict(v):
    return v if isinstance(v, dict) else {}


def load_snapshot(path):
    """(snapshot dict | None, 'ok' | 'missing' | 'corrupt')."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "missing"
    except Exception:
        return None, "corrupt"
    if not isinstance(data, dict) or not isinstance(data.get("brands"), dict) \
            or not isinstance(data.get("total"), dict):
        return None, "corrupt"
    return data, "ok"


def snapshot_placeholder(state):
    if state == "missing":
        return NO_SNAPSHOT_TEXT
    if state == "corrupt":
        return CORRUPT_SNAPSHOT_TEXT
    return None


def fmt_num(v):
    return f"{v:,}" if _is_count(v) else "—"


def fmt_usd(v):
    return f"${v:,.2f}" if _is_number(v) else "—"


def fmt_pct(v):
    """An already-computed percentage (e.g. 41.3) -> '41.3%'."""
    return f"{v:.1f}%" if _is_number(v) else "—"


def share_pct(part, whole):
    """part / whole as '12.3%'; '—' when either is unknown or whole is 0."""
    if not _is_count(part) or not _is_count(whole) or whole <= 0:
        return "—"
    return f"{part / whole * 100:.1f}%"


def _brand(snap, b):
    return _dict(_dict(_dict(snap).get("brands")).get(b))


def _total(snap):
    return _dict(_dict(snap).get("total"))


def _kpi_note(snap, key):
    notes = _dict(_dict(_dict(snap).get("notes")).get(key))
    parts = [f"{b}: {notes[b]}" for b in OVERVIEW_BRANDS if isinstance(notes.get(b), str)]
    return " · ".join(parts) or None


def kpi_cards(snap):
    """Four KPI cards: the total, the M&S / H&M / Other split and the share
    of total SKUs."""
    total_skus = _total(snap).get("offers")
    cards = []
    for key, title in KPI_SPEC:
        value = _total(snap).get(key)
        split = [(b, fmt_num(_brand(snap, b).get(key))) for b in OVERVIEW_BRANDS]
        share = share_pct(value, total_skus)
        cards.append({"key": key, "title": title, "value": fmt_num(value),
                      "split": split, "sub": " · ".join(f"{b} {v}" for b, v in split),
                      "share": share, "share_text": f"{share} of total SKUs",
                      "note": _kpi_note(snap, key)})
    return cards


BRAND_TABLE_COLUMNS = (("brand", "Brand", 90), ("skus", "SKUs", 90), ("for_sale", "For sale", 90))


def brand_rows(snap):
    rows = []
    for name, d in [(b, _brand(snap, b)) for b in OVERVIEW_BRANDS] + [("Total", _total(snap))]:
        skus, for_sale = d.get("offers"), d.get("for_sale")
        rows.append({"brand": name, "skus": skus if _is_count(skus) else None,
                     "for_sale": for_sale if _is_count(for_sale) else None})
    return rows


WAREHOUSE_TABLE_COLUMNS = (("warehouse", "Warehouse", 120), ("skus", "Live SKUs", 80),
                           ("avg", "Avg price", 80), ("min", "Min price", 80), ("max", "Max price", 80))


def warehouse_rows(snap):
    """(columns, rows); rows are [label, live SKUs, avg, min, max], already
    formatted (USD prices, '—' when null). Fixed labels first (when present),
    then the other labels sorted. [] when unknown."""
    columns = [c[1] for c in WAREHOUSE_TABLE_COLUMNS]
    wh = _dict(snap).get("warehouses")
    if not isinstance(wh, dict):
        return columns, []
    labels = [k for k in WAREHOUSE_FIXED if k in wh]
    labels += sorted(str(k) for k in wh if k not in WAREHOUSE_FIXED)
    rows = []
    for label in labels:
        d = _dict(wh.get(label))
        rows.append([label, fmt_num(d.get("skus")), fmt_usd(d.get("avg_price")),
                     fmt_usd(d.get("min_price")), fmt_usd(d.get("max_price"))])
    return columns, rows


def warehouse_note(snap):
    return None if isinstance(_dict(snap).get("warehouses"), dict) else WAREHOUSE_UNKNOWN_NOTE


def parse_iso(s):
    """Naive local datetime, or None. Aware strings are converted to local."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        t = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    if t.tzinfo is not None:
        try:
            t = t.astimezone().replace(tzinfo=None)
        except Exception:
            return None
    return t


def is_stale(built_at, now, hours=SNAPSHOT_STALE_HOURS):
    """Older than `hours` (exactly `hours` is still fresh, like catalog_status.is_fresh)."""
    t = parse_iso(built_at)
    if t is None or not isinstance(now, datetime):
        return True
    return now - t > timedelta(hours=hours)


def fmt_when(s):
    t = parse_iso(s)
    return f"{t:%Y-%m-%d %H:%M}" if t else "—"


def data_as_of(snap, now):
    """(text, color_key): color_key is 'warning' when stale, else None."""
    built = _dict(snap).get("built_at")
    text = f"data as of {fmt_when(built)}"
    if is_stale(built, now):
        return text + f" (stale: older than {SNAPSHOT_STALE_HOURS}h)", "warning"
    return text, None


def metrics_status(snap, now):
    """(text, color_key, stale) for overview_metrics' age (metrics_built_at).
    Null / unparsable -> NO_METRICS_TEXT, not stale. Older than
    SNAPSHOT_STALE_HOURS -> 'warning', stale."""
    built = _dict(snap).get("metrics_built_at")
    if parse_iso(built) is None:
        return NO_METRICS_TEXT, None, False
    text = f"report of {fmt_when(built)}"
    if is_stale(built, now if isinstance(now, datetime) else datetime.now()):
        return text + f" (stale: older than {SNAPSHOT_STALE_HOURS}h)", "warning", True
    return text, None, False


def _metrics_card(title, snap, now, key, empty_text):
    """(card, block | None). block is None when there's nothing to list."""
    status, color, stale = metrics_status(snap, now)
    card = {"title": title, "caption": None, "status": status, "status_color": color,
            "stale": stale, "message": None, "lines": []}
    if status == NO_METRICS_TEXT:
        card["status"], card["message"] = None, NO_METRICS_TEXT
        return card, None
    block = _dict(snap).get(key)
    if not isinstance(block, dict):
        card["message"] = empty_text
        return card, None
    return card, block


def _empty_card(title, caption=None):
    return {"title": title, "caption": caption, "status": None, "status_color": None,
            "stale": False, "message": NO_METRICS_TEXT, "lines": []}


def campaign_card(snap, now=None):
    """Campaigns card: live SKUs in campaigns, avg / min cost/sale %."""
    try:
        card, c = _metrics_card(CAMPAIGNS_TITLE, snap, now, "campaigns",
                                "No campaign data in the last daily report")
        if c is None:
            return card
        card["lines"] = [("Live SKUs in campaigns", fmt_num(c.get("live_skus"))),
                         ("Avg cost/sale", fmt_pct(c.get("avg_ratio_pct"))),
                         ("Min cost/sale", fmt_pct(c.get("min_ratio_pct")))]
        n = c.get("ratio_count")
        if _is_count(n):
            card["caption"] = f"cost/sale over {n:,} SKUs with an M&S cost"
        return card
    except Exception:
        return _empty_card(CAMPAIGNS_TITLE)


def sales_card(snap, now=None):
    """'Sales last 34 hours' card: revenue, orders, SKUs (units), avg
    cost/sale, small / regular %, brand sale %, top sold category."""
    try:
        card, s = _metrics_card(SALES_TITLE, snap, now, "sales",
                                "No sales data in the last daily report")
        win = s if isinstance(s, dict) else {}
        card["caption"] = (f"updated daily 10:00 · {fmt_when(win.get('window_from'))}"
                           f" → {fmt_when(win.get('window_to'))}")
        if not card["stale"]:
            card["status"] = None  # the caption already dates it; only a stale age is shown
        if s is None:
            return card
        units = s.get("units")
        skus_text = fmt_num(s.get("skus")) + (f" ({units:,} units)" if _is_count(units) else "")
        wh = _dict(s.get("warehouse_pct"))
        wh_text = f"{fmt_pct(wh.get('small'))} / {fmt_pct(wh.get('regular'))}"
        unknown = wh.get("unknown")
        if _is_number(unknown) and unknown > 0:
            wh_text += f" (unknown {fmt_pct(unknown)})"
        bp = _dict(s.get("brand_pct"))
        brand_text = " · ".join(f"{b} {fmt_pct(bp.get(b))}" for b in OVERVIEW_BRANDS)
        top = s.get("top_category")
        top_text = "—"
        if isinstance(top, dict) and isinstance(top.get("label"), str) and top["label"]:
            top_text = top["label"]
            if _is_count(top.get("units")):
                top_text += f" ({top['units']:,} units)"
        card["lines"] = [("Revenue", fmt_usd(s.get("revenue_usd"))),
                         ("Orders", fmt_num(s.get("orders"))),
                         ("SKUs", skus_text),
                         ("Avg cost/sale", fmt_pct(s.get("avg_ms_cost_pct"))),
                         ("Small / regular", wh_text),
                         ("Brand sale %", brand_text),
                         ("Top sold category", top_text)]
        return card
    except Exception:
        return _empty_card(SALES_TITLE, "updated daily 10:00 · — → —")


def sources_warnings(snap):
    out = []
    for name, info in sorted(_dict(_dict(snap).get("sources")).items()):
        st = _dict(info).get("status")
        if st != "ok":
            out.append(f"{name} {st or 'unknown'}")
    return out


# --- Tabs (pure registration hook; no tkinter at module level) --------------------

APP_TITLE = "Ozon Sales"
TAB_OVERVIEW = "Overview"
TAB_DAILY = "Daily tasks"
TAB_PRIORITIZATION = "New Product Adding"
TAB_NEW_PRODUCT_ADDING = TAB_PRIORITIZATION
TAB_NAMES = (TAB_OVERVIEW, TAB_DAILY, TAB_PRIORITIZATION)
DEFAULT_TAB = TAB_PRIORITIZATION  # New Product Adding opens first
DAILY_REFRESH_MS = 60_000  # Daily tasks auto-refresh interval
DAILY_FIRST_REFRESH_MS = 100

TAB_BUILDERS = {}  # name -> builder(app, parent) -> object; insertion order = tab order


def register_tab(name, builder=None):
    """Registers builder(app, parent) -> object as the tab `name`. Works as a
    plain call or as a decorator (@register_tab("X")). Re-registering a name
    replaces its builder but keeps its position. Returns the builder."""
    if builder is None:
        def deco(fn):
            return register_tab(name, fn)
        return deco
    TAB_BUILDERS[name] = builder
    return builder


def tab_names():
    return list(TAB_BUILDERS)


# --- UI --------------------------------------------------------------------------

class _WidgetHelpers:
    """Shared widget helpers; needs self.tk and self.dpi."""

    def px(self, n):
        return int(round(n * self.dpi))

    def label(self, parent, text, size=10, color=FG, bold=False, anchor="w", **kw):
        return self.tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                             font=(FONT, size, "bold" if bold else "normal"), anchor=anchor,
                             justify="left", **kw)

    def button(self, parent, text, command, accent=False):
        return self.tk.Button(parent, text=text, command=command, bg=STATUS["running"] if accent else LINE,
                              fg=FG, activebackground="#4a4a46", activeforeground=FG, relief="flat",
                              font=(FONT, 9, "bold" if accent else "normal"), padx=10, pady=4,
                              bd=0, cursor="hand2", disabledforeground=DIM)


STEP_COLUMNS = (("name", "Step", 420), ("result", "Result", 260), ("seconds", "Duration", 120))
DAILY_FOOTNOTE = "View only — scheduled tasks are managed in Windows Task Scheduler."


class DailyTasksTab(_WidgetHelpers):
    """View-only status of the scheduled tasks and the latest daily_run.

    task_status.collect_status() runs on a daemon thread and its result comes
    back through app.post; collects never overlap (self.busy) and only one
    auto-refresh timer is ever pending. Refresh is the tab's only button."""

    def __init__(self, app, parent):
        self.app = app
        self.tk, self.ttk = app.tk, app.ttk
        self.dpi = app.dpi
        self.busy = False
        self._after_id = None
        self.status = None
        tk = self.tk

        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        self.label(head, TAB_DAILY, 14, bold=True).pack(side="left")
        self.refresh_btn = self.button(head, "Refresh", self.refresh)
        self.refresh_btn.pack(side="right")
        self.collected_label = self.label(head, f"collected {DASH}", 9, MUTED)
        self.collected_label.pack(side="right", padx=(0, 10))

        # packed (before the task sections) only while stale_banner() has text
        self.banner = tk.Label(outer, text="", bg=STATUS["warning"], fg=BG, font=(FONT, 9, "bold"),
                               anchor="w", padx=8, pady=4)

        # one card per scheduled task (build_task_sections), vertically scrollable
        self.sections_wrap = tk.Frame(outer, bg=BG)
        self.sections_wrap.pack(fill="both", expand=True, pady=(10, 0))
        self.sections_canvas = tk.Canvas(self.sections_wrap, bg=BG, highlightthickness=0, bd=0,
                                         height=self.px(300))
        sb = self.ttk.Scrollbar(self.sections_wrap, orient="vertical",
                                command=self.sections_canvas.yview)
        self.sections_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.sections_canvas.pack(side="left", fill="both", expand=True)
        self.sections = tk.Frame(self.sections_canvas, bg=BG)
        self._sections_win = self.sections_canvas.create_window((0, 0), window=self.sections,
                                                                anchor="nw")
        self.sections.bind("<Configure>", lambda e: self.sections_canvas.configure(
            scrollregion=self.sections_canvas.bbox("all")))
        self.sections_canvas.bind("<Configure>", lambda e: self.sections_canvas.itemconfigure(
            self._sections_win, width=e.width))
        self.sections_canvas.bind("<Enter>", lambda e: self.sections_canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self.sections_canvas.bind("<Leave>", lambda e: self.sections_canvas.unbind_all("<MouseWheel>"))

        card = tk.Frame(outer, bg=CARD, padx=10, pady=8)
        card.pack(fill="x", pady=(10, 0))
        self.label(card, "Latest daily run", 10, FG, True).pack(anchor="w")
        self.summary = tk.Frame(card, bg=CARD)
        self.summary.pack(fill="x", pady=(4, 0))
        self.attention = self.label(card, "", 9, STATUS["warning"], wraplength=self.px(1000))
        self.attention.pack(anchor="w", pady=(4, 0))

        steps_wrap = tk.Frame(outer, bg=BG)
        steps_wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.steps = self._tree(steps_wrap, STEP_COLUMNS, 8)

        # packed (before the footnote) only while there are errors
        self.errors = self.label(outer, "", 9, STATUS["critical"], wraplength=self.px(1000))
        self.footnote = self.label(outer, DAILY_FOOTNOTE, 8, MUTED)
        self.footnote.pack(anchor="w", pady=(6, 0))

        self._schedule(DAILY_FIRST_REFRESH_MS)

    def _tree(self, parent, columns, height):
        tree = self.ttk.Treeview(parent, columns=[c[0] for c in columns], show="headings",
                                 selectmode="browse", height=height)
        for key, heading, width in columns:
            tree.heading(key, text=heading)
            tree.column(key, width=self.px(width), anchor="w", stretch=True)
        for level, color in STATUS.items():
            tree.tag_configure(level, foreground=color)
        sb = self.ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

    def _on_wheel(self, event):
        try:
            self.sections_canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1),
                                              "units")
        except Exception:
            pass

    def _render_sections(self, status):
        for w in self.sections.winfo_children():
            w.destroy()
        for sec in build_task_sections(status):
            card = self.tk.Frame(self.sections, bg=CARD, padx=10, pady=8)
            card.pack(fill="x", pady=(0, 8))
            self.label(card, sec["title"], 11, level_color(sec["level"]), True).pack(anchor="w")
            self.label(card, f"next run {sec['next_run']} · last run {sec['last_run']} · "
                             f"result {sec['result']}", 9, MUTED).pack(anchor="w", pady=(2, 0))
            grid = self.tk.Frame(card, bg=CARD)
            grid.pack(fill="x", pady=(4, 0))
            for i, (lbl, value, level) in enumerate(sec["metrics"]):
                r, c = divmod(i, 3)
                self.label(grid, lbl, 8, MUTED).grid(row=r * 2, column=c, sticky="w", padx=(0, 24))
                self.label(grid, value, 10, level_color(level), True).grid(
                    row=r * 2 + 1, column=c, sticky="w", padx=(0, 24))
            if sec.get("note"):
                self.label(card, sec["note"], 9, level_color(sec.get("note_level") or "warning")
                           ).pack(anchor="w", pady=(4, 0))
        self.sections_canvas.yview_moveto(0)

    # ---------- refresh (worker thread -> app.post -> UI thread) ----------
    def _cancel_timer(self):
        if self._after_id is not None:
            try:
                self.app.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule(self, ms):
        self._cancel_timer()
        self._after_id = self.app.root.after(ms, self.refresh)

    def refresh(self):
        """Button + timer entry point. Does nothing while a collect is in flight."""
        if self.busy:
            return  # the in-flight collect's _apply reschedules the timer
        self._cancel_timer()
        self.busy = True
        self.refresh_btn.configure(state="disabled")
        self.collected_label.configure(text="collecting…")

        def work():  # worker thread: no Tk calls here
            self.app.post(self._apply, collect_daily_status())

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, result):
        try:
            self.status = result
            self.render(result)
        except Exception as e:
            print(f"Daily tasks render failed: {e}")
        finally:
            self.busy = False
            self.refresh_btn.configure(state="normal")
            self._schedule(DAILY_REFRESH_MS)

    # ---------- drawing (UI thread only) ----------
    def render(self, status):
        self.collected_label.configure(text=collected_text(status))

        banner = stale_banner(status)
        if banner:
            self.banner.configure(text=banner)
            self.banner.pack(fill="x", pady=(8, 0), before=self.sections_wrap)
        else:
            self.banner.pack_forget()

        self._render_sections(status)

        dr = _as_dict(_as_dict(status).get("daily_run"))
        for w in self.summary.winfo_children():
            w.destroy()
        for i, (lbl, value, level) in enumerate(build_run_summary(dr)):
            r, c = divmod(i, 4)
            self.label(self.summary, lbl, 8, MUTED).grid(row=r * 2, column=c, sticky="w", padx=(0, 24))
            self.label(self.summary, value, 10, level_color(level), True).grid(
                row=r * 2 + 1, column=c, sticky="w", padx=(0, 24))
        self.attention.configure(text="\n".join(f"• {t}" for t in attention_items(dr)))

        self.steps.delete(*self.steps.get_children())
        for row in build_step_rows(dr):
            self.steps.insert("", "end", values=[row[k] for k, _, _ in STEP_COLUMNS], tags=(row["level"],))

        lines = error_lines(status)
        if lines:
            self.errors.configure(text="\n".join(lines))
            self.errors.pack(anchor="w", pady=(6, 0), before=self.footnote)
        else:
            self.errors.pack_forget()


build_daily_tasks_tab = DailyTasksTab  # backward-compatible name


class OverviewTab(_WidgetHelpers):
    """Account status from account_snapshot.json (snapshot v2)."""

    def __init__(self, app, parent):
        self.app = app
        self.tk, self.ttk = app.tk, app.ttk
        self.dpi = app.dpi
        tk = self.tk
        self.busy = False
        self.snap, self.state = None, "missing"
        self.status_var = tk.StringVar(value="Loaded cached snapshot.")

        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        self.label(head, "Overview", 14, bold=True).pack(side="left")
        self.asof_label = self.label(head, "", 9, MUTED)
        self.asof_label.pack(side="right")

        act = tk.Frame(outer, bg=BG)
        act.pack(side="bottom", fill="x", pady=(6, 0))
        self.refresh_btn = self.button(act, "Refresh", self.on_refresh)
        self.refresh_btn.pack(side="left")
        tk.Label(act, textvariable=self.status_var, bg=BG, fg=MUTED, font=(FONT, 8),
                 anchor="w").pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.label(act, "Refresh runs account_snapshot.py (offline, local files only).", 8, MUTED).pack(side="right")

        self.body = tk.Frame(outer, bg=BG)
        self.body.pack(fill="both", expand=True)

        self.load_local()

    # ---------- data ----------
    def load_local(self):
        """Re-reads the local snapshot and redraws."""
        self.snap, self.state = load_snapshot(HERE / ACCOUNT_SNAPSHOT_FILE)
        self.render()

    # ---------- drawing ----------
    def _tree(self, parent, columns, height):
        """columns: [(key, heading, width)]; returns a Treeview packed in parent."""
        tree = self.ttk.Treeview(parent, columns=[c[0] for c in columns], show="headings",
                                 selectmode="none", height=height)
        for i, (key, heading, width) in enumerate(columns):
            tree.heading(key, text=heading)
            tree.column(key, width=self.px(width), anchor="w" if i == 0 else "e", stretch=True)
        tree.tag_configure("total", font=(FONT, 9, "bold"))
        tree.pack(fill="both", expand=True)
        return tree

    def _section(self, parent, title, row, col, colspan=1, rowspan=1):
        f = self.tk.Frame(parent, bg=CARD, padx=8, pady=6)
        f.grid(row=row, column=col, columnspan=colspan, rowspan=rowspan, sticky="nsew",
               padx=(0 if col == 0 else 6, 0), pady=(8, 0))
        self.label(f, title, 9, MUTED, True).pack(anchor="w", pady=(0, 4))
        return f

    def render(self):
        """Tears the body down and rebuilds it from self.snap."""
        for w in self.body.winfo_children():
            w.destroy()
        now = datetime.now()
        if self.state == "ok":
            text, color = data_as_of(self.snap, now)
            self.asof_label.configure(text=text, fg=STATUS[color] if color else MUTED)
        else:
            self.asof_label.configure(text="", fg=MUTED)

        grid = self.tk.Frame(self.body, bg=BG)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)

        if self.state != "ok":
            self.label(grid, snapshot_placeholder(self.state), 11, STATUS["warning"]).grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(10, 0))
            return
        warn = sources_warnings(self.snap)
        if warn:
            self.label(grid, "Sources not ok: " + ", ".join(warn), 9, STATUS["warning"]).grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._render_cards(grid)
        self._render_brands(grid)
        self._render_warehouses(grid)
        self._render_metrics_card(grid, campaign_card(self.snap, now), 2)
        self._render_metrics_card(grid, sales_card(self.snap, now), 3)

    def _render_cards(self, grid):
        cards = self.tk.Frame(grid, bg=BG)
        cards.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for i, c in enumerate(kpi_cards(self.snap)):
            f = self.tk.Frame(cards, bg=CARD, padx=10, pady=8)
            f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            cards.columnconfigure(i, weight=1, uniform="k")
            self.label(f, c["title"], 8, MUTED).pack(anchor="w")
            self.label(f, c["value"], 15, FG, True).pack(anchor="w")
            self.label(f, c["sub"], 8, MUTED, wraplength=self.px(300)).pack(anchor="w")
            self.label(f, c["share_text"], 8, MUTED).pack(anchor="w")
            if c["note"]:
                self.label(f, c["note"], 7, DIM, wraplength=self.px(300)).pack(anchor="w")

    def _render_brands(self, grid):
        f = self._section(grid, "Brands", 2, 0)
        tree = self._tree(f, BRAND_TABLE_COLUMNS, 4)
        for row in brand_rows(self.snap):
            tree.insert("", "end", values=[row["brand"], fmt_num(row["skus"]), fmt_num(row["for_sale"])],
                        tags=("total",) if row["brand"] == "Total" else ())

    def _render_warehouses(self, grid):
        f = self._section(grid, "Warehouses (prices in USD)", 3, 0)
        note = warehouse_note(self.snap)
        if note:
            self.label(f, note, 9, STATUS["warning"]).pack(anchor="w")
            return
        _, rows = warehouse_rows(self.snap)
        tree = self._tree(f, WAREHOUSE_TABLE_COLUMNS, max(3, min(len(rows), 6)))
        for r in rows:
            tree.insert("", "end", values=r)

    def _render_metrics_card(self, grid, card, row):
        f = self._section(grid, card["title"], row, 1)
        if card["caption"]:
            self.label(f, card["caption"], 8, MUTED, wraplength=self.px(320)).pack(anchor="w")
        if card["status"]:
            color = card["status_color"]
            self.label(f, card["status"], 8, STATUS[color] if color else MUTED,
                       wraplength=self.px(320)).pack(anchor="w")
        if card["message"]:
            self.label(f, card["message"], 9, MUTED, wraplength=self.px(320)).pack(anchor="w", pady=(4, 0))
        if card["lines"]:
            table = self.tk.Frame(f, bg=CARD)
            table.pack(fill="x", pady=(4, 0))
            table.columnconfigure(1, weight=1)
            for i, (name, value) in enumerate(card["lines"]):
                self.label(table, name, 9, MUTED).grid(row=i, column=0, sticky="w")
                self.label(table, value, 9, FG, True, anchor="e",
                           wraplength=self.px(300)).grid(row=i, column=1, sticky="e", padx=(10, 0))

    # ---------- actions (the subprocess runs in a thread) ----------
    def on_refresh(self):
        if self.busy:
            return
        self.busy = True
        self.refresh_btn.configure(state="disabled")
        self.status_var.set(f"Running account_snapshot.py since {datetime.now():%H:%M}…")

        def work():
            rc, out = run_script("account_snapshot")
            self.app.post(self._refresh_done, rc, out)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_done(self, rc, out):
        self.busy = False
        self.refresh_btn.configure(state="normal")
        ok = "ok" if rc == 0 else ("failed" if rc is not None else "not run")
        self.status_var.set(f"Refreshed {datetime.now():%H:%M}: account_snapshot.py {ok}"
                            + ("" if rc == 0 else f" ({tail(out, 1)})"))
        self.load_local()


build_overview_tab = OverviewTab  # backward-compatible name


class PrioritizationTab(_WidgetHelpers):
    """New Product Adding: quota cards, the URL-target table (priority, URLs
    to add, Add all / Add half, Save, Add a new website), the daily-plan
    editor, the collapsible offer-allocation table, actions."""

    def __init__(self, app, parent):
        self.app = app
        self.tk, self.ttk = app.tk, app.ttk
        self.dpi = app.dpi
        tk = self.tk
        self.today = daily_plan.utc_today()
        self.plan = daily_plan.load_plan(str(HERE / PLAN_FILE))
        self.day_key = first_planned_day(self.plan, self.today)
        self.day = get_day(self.plan, self.day_key)
        self.dirty = False
        self.quota = None
        self.quota_state = "checking"
        self.created_by_cat = None
        self.busy = set()
        self.rows = []
        self.editor = None
        self.np_plan = new_product_plan.load_plan(str(HERE / NP_PLAN_FILE))
        self.np_saved = copy.deepcopy(self.np_plan)
        self.np_rows = []
        self.np_vars = {}  # (brand, category) -> (StringVar, Entry, eligible)
        self._np_render_pending = False
        self.alloc_open = True  # the table the 03:00 new-products job follows -- shown by default
        self.hm_inventory = None
        self.website_win = None

        self.total_var = tk.StringVar(value=str(self.day["total"]))
        self.fill_var = tk.BooleanVar(value=self.day["fill_remainder"])
        self.day_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Loaded cached numbers.")
        self.walk_var = tk.StringVar(value="")

        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        self.label(head, TAB_NEW_PRODUCT_ADDING, 14, bold=True).pack(side="left")
        self.built_label = self.label(head, "", 9, MUTED)
        self.built_label.pack(side="right")
        self.qp_vars = {}        # (brand, label) -> (StringVar, Entry)
        self.qp_dirty = False
        self.qp_date = None
        self._qp_loading = False
        self._build_quick_plan(outer)

        # Everything else (cards, URL notes, per-day editor, allocation table) -- collapsed.
        self.adv_open = False
        self.adv_toggle = tk.Button(outer, text="", command=self.toggle_adv, bg=BG, fg=MUTED,
                                    activebackground=BG, activeforeground=FG, relief="flat", bd=0,
                                    font=(FONT, 9, "bold"), anchor="w", cursor="hand2")
        self.adv_toggle.pack(fill="x", pady=(10, 0))
        self.adv_body = tk.Frame(outer, bg=BG)
        self.cards = tk.Frame(self.adv_body, bg=BG)
        self.cards.pack(fill="x", pady=(10, 0))
        self._build_np_table(self.adv_body)
        self._build_editor(self.adv_body)
        self._build_alloc_section(self.adv_body)
        self._update_adv_toggle()
        self._build_actions(outer)

        self.load_local()
        self.app.root.after(50, self.start_background_reads)
        self.app.root.after(60_000, self._watch_day)

    # ---------- thread hand-off ----------
    def post(self, fn, *args):
        """Called from worker threads: hands fn(*args) to the shell's queue."""
        self.app.post(fn, *args)

    # ---------- helpers ----------
    def card(self, col, title, value, sub, sub_color=MUTED):
        f = self.tk.Frame(self.cards, bg=CARD, padx=10, pady=8)
        f.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
        self.cards.columnconfigure(col, weight=1, uniform="c")
        self.label(f, title, 8, MUTED).pack(anchor="w")
        self.label(f, value, 15, FG, True).pack(anchor="w")
        self.label(f, sub, 8, sub_color, wraplength=self.px(220)).pack(anchor="w")

    # ---------- quick plan (the simple way to plan the next run) ----------
    def _build_quick_plan(self, parent):
        tk = self.tk
        f = tk.Frame(parent, bg=CARD, padx=12, pady=10)
        f.pack(fill="both", expand=True, pady=(10, 0))
        self.qp_title = self.label(f, "", 12, FG, True)
        self.qp_title.pack(anchor="w")
        self.label(f, "1. Type how many offers to add in each category (leave empty for none).   "
                      "2. Press Save.   The 03:00 job adds exactly these -- the total is the sum.",
                   9, MUTED, wraplength=self.px(1100)).pack(anchor="w", pady=(2, 6))
        wrap = tk.Frame(f, bg=CARD)
        bottom = tk.Frame(f, bg=CARD)
        self.qp_msg = self.label(f, "", 9, MUTED, wraplength=self.px(1100))
        self.qp_msg.pack(side="bottom", anchor="w", pady=(6, 0))
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        wrap.pack(fill="both", expand=True)
        self.qp_canvas = tk.Canvas(wrap, bg=CARD, highlightthickness=0, bd=0, height=self.px(240))
        sb = self.ttk.Scrollbar(wrap, orient="vertical", command=self.qp_canvas.yview)
        self.qp_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.qp_canvas.pack(side="left", fill="both", expand=True)
        self.qp_grid = tk.Frame(self.qp_canvas, bg=CARD)
        self._qp_win = self.qp_canvas.create_window((0, 0), window=self.qp_grid, anchor="nw")
        self.qp_grid.bind("<Configure>", lambda e: self.qp_canvas.configure(scrollregion=self.qp_canvas.bbox("all")))
        self.qp_canvas.bind("<Enter>", lambda e: self.qp_canvas.bind_all("<MouseWheel>", self._on_qp_wheel))
        self.qp_canvas.bind("<Leave>", lambda e: self.qp_canvas.unbind_all("<MouseWheel>"))
        self.qp_sum = self.label(bottom, "", 11, FG, True)
        self.qp_sum.pack(side="left")
        self.qp_save_btn = self.button(bottom, "Save plan", self.on_qp_save, accent=True)
        self.qp_save_btn.pack(side="right")
        self.button(bottom, "Clear all", self.on_qp_clear).pack(side="right", padx=(0, 6))
        self.button(bottom, "Copy last plan", self.on_qp_copy).pack(side="right", padx=(0, 6))

    def _on_qp_wheel(self, event):
        try:
            self.qp_canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        except Exception:
            pass

    def _qp_target_date(self):
        return next_new_products_day(load_json(HERE / NEW_PRODUCTS_STATUS_FILE, {}) or {})

    def _qp_render(self, force=False):
        """(Re)builds the boxes for the next run's date, prefilled from its saved plan. Keeps the
        owner's unsaved typing unless force or the date moved."""
        date = self._qp_target_date()
        if self.qp_dirty and not force and date == self.qp_date:
            self._qp_update()
            return
        self.qp_date = date
        tomorrow = (_date.fromisoformat(self.today) + timedelta(days=1)).isoformat()
        when = "tomorrow" if date == tomorrow else ("today" if date == self.today else date)
        self.qp_title.configure(text=f"Plan the next run: {when}, {date} -- starts 03:00 Moscow")
        day = get_day(self.plan, date) if has_override(self.plan, date) else \
            {"total": 0, "allocations": {}, "fill_remainder": False}
        rows = quick_plan_rows(build_rows(self.inventory, self.catalog, day, {}, self.hm_inventory, {}), day)
        for w in self.qp_grid.winfo_children():
            w.destroy()
        self.qp_vars = {}
        tk = self.tk
        for c, (text, width) in enumerate((("Brand", 8), ("Category", 16), ("Available to add", 16),
                                           ("How many", 10))):
            tk.Label(self.qp_grid, text=text, bg=CARD, fg=MUTED, font=(FONT, 9, "bold"), width=width,
                     anchor="w").grid(row=0, column=c, sticky="w", padx=(0, 8), pady=(0, 4))
        self._qp_loading = True
        for i, r in enumerate(rows, start=1):
            tk.Label(self.qp_grid, text=r["brand"], bg=CARD, fg=FG, font=(FONT, 10), anchor="w").grid(
                row=i, column=0, sticky="w", padx=(0, 8))
            tk.Label(self.qp_grid, text=r["category"], bg=CARD, fg=FG, font=(FONT, 10), anchor="w").grid(
                row=i, column=1, sticky="w", padx=(0, 8))
            tk.Label(self.qp_grid, text=f"{r['available']:,}" if r["available"] else "--", bg=CARD, fg=MUTED,
                     font=(FONT, 10), anchor="w").grid(row=i, column=2, sticky="w", padx=(0, 8))
            var = tk.StringVar(value=str(r["planned"]) if r["planned"] else "")
            ent = tk.Entry(self.qp_grid, textvariable=var, width=8, bg="#3f3f3b", fg=FG, insertbackground=FG,
                           relief="solid", bd=1, highlightthickness=0, font=(FONT, 10), justify="right")
            ent.grid(row=i, column=3, sticky="w", pady=1)
            var.trace_add("write", lambda *a: self._qp_changed())
            self.qp_vars[(r["brand"], r["category"])] = (var, ent, r["available"])
        self._qp_loading = False
        self.qp_dirty = False
        saved = has_override(self.plan, date)
        self.qp_msg.configure(text=(f"Saved plan for {date} loaded." if saved else
                                    f"Nothing planned for {date} yet -- the job will add nothing until you save."),
                              fg=MUTED if saved else STATUS["warning"])
        self._qp_update()

    def _qp_values(self):
        return {k: parse_quick_value(var.get()) for k, (var, _e, _a) in self.qp_vars.items()}

    def _qp_changed(self):
        if self._qp_loading:
            return
        self.qp_dirty = True
        self.qp_msg.configure(text="Not saved yet -- press Save plan.", fg=STATUS["warning"])
        self._qp_update()

    def _qp_update(self):
        values = self._qp_values()
        for k, (var, ent, avail) in self.qp_vars.items():
            v = values[k]
            ent.configure(bg=STATUS["critical"] if v is None else ("#6a5626" if v and v > avail else
                                                                   ("#2f4a36" if v else "#3f3f3b")))
        _day, errors = quick_plan_day(values)
        self.qp_sum.configure(text=quick_plan_summary(values, self.quota),
                              fg=STATUS["critical"] if errors else FG)
        self.qp_save_btn.configure(state="disabled" if errors else "normal")

    def on_qp_save(self):
        date = self.qp_date
        day, errors = quick_plan_day(self._qp_values())
        if errors:
            self.qp_msg.configure(text="Not saved: " + "; ".join(errors), fg=STATUS["critical"])
            return
        saved, errs = save_plan_from_ui(set_day(self.plan, date, day), str(HERE / PLAN_FILE))
        if errs:
            self.qp_msg.configure(text="Not saved: " + "; ".join(errs), fg=STATUS["critical"])
            return
        self.plan = saved
        if self.day_key == date or not self.dirty:
            self.day_key = date
            self.day = get_day(self.plan, date)
            self.dirty = False
            self.total_var.set(str(self.day["total"]))
            self.fill_var.set(self.day["fill_remainder"])
            self.day_var.set(day_label(self.day_key, self.today))
        self.qp_dirty = False
        split = _plan_split_text(day)
        over = [f"{b} {l}" for (b, l), (_v, _e, a) in self.qp_vars.items()
                if (self._qp_values().get((b, l)) or 0) > a]
        msg = (f"Saved ({datetime.now():%H:%M}). On {date} at 03:00 the job adds {day['total']} offers"
               + (f": {split}" if split else " (nothing)") + ".")
        if over:
            msg += f"  Note: more than available in {', '.join(over)} -- the job adds what it finds."
        self.qp_msg.configure(text=msg, fg=STATUS["good"])
        self.render()

    def on_qp_clear(self):
        for var, _e, _a in self.qp_vars.values():
            var.set("")

    def on_qp_copy(self):
        day, key = last_planned_day(self.plan, self.qp_date or self.today)
        if not day:
            self.qp_msg.configure(text="No earlier plan to copy.", fg=STATUS["warning"])
            return
        missing = []
        for var, _e, _a in self.qp_vars.values():
            var.set("")
        for brand, cats in day["allocations"].items():
            for label, n in cats.items():
                if (brand, label) in self.qp_vars:
                    self.qp_vars[(brand, label)][0].set(str(n))
                else:
                    missing.append(f"{brand} {label}")
        self.qp_msg.configure(text=f"Copied the plan of {key}. Press Save plan to use it."
                              + (f"  Not copied (nothing available now): {', '.join(missing)}" if missing else ""),
                              fg=STATUS["warning"])

    def _update_adv_toggle(self):
        self.adv_toggle.configure(text=("▾ " if self.adv_open else "▸ ") +
                                  "Advanced: other days, default plan, URL notes, details per category")

    def toggle_adv(self):
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv_body.pack(fill="both", expand=True, after=self.adv_toggle)
        else:
            self._close_editor()
            self.adv_body.pack_forget()
        self._update_adv_toggle()

    # ---------- layout ----------
    def _build_np_table(self, parent):
        """Scrollable grid (Canvas + Frame, mouse wheel like DailyTasksTab)."""
        tk = self.tk
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", pady=(10, 0))
        self.label(bar, "URLs to add per sub category", 11, FG, True).pack(side="left")
        self.website_btn = self.button(bar, "Add a new website", self.open_website_form)
        self.website_btn.pack(side="right")
        self.np_save_btn = self.button(bar, "Save", self.on_np_save, accent=True)
        self.np_save_btn.pack(side="right", padx=(0, 6))
        self.np_note = self.label(bar, "", 9, MUTED)
        self.np_note.pack(side="right", padx=(0, 10))

        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.np_canvas = tk.Canvas(wrap, bg=CARD, highlightthickness=0, bd=0, height=self.px(220))
        sb = self.ttk.Scrollbar(wrap, orient="vertical", command=self.np_canvas.yview)
        self.np_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.np_canvas.pack(side="left", fill="both", expand=True)
        self.np_grid = tk.Frame(self.np_canvas, bg=CARD, padx=8, pady=6)
        self._np_win = self.np_canvas.create_window((0, 0), window=self.np_grid, anchor="nw")
        self.np_grid.bind("<Configure>", lambda e: self.np_canvas.configure(
            scrollregion=self.np_canvas.bbox("all")))
        self.np_canvas.bind("<Configure>", lambda e: self.np_canvas.itemconfigure(
            self._np_win, width=e.width))
        self.np_canvas.bind("<Enter>", lambda e: self.np_canvas.bind_all("<MouseWheel>", self._on_np_wheel))
        self.np_canvas.bind("<Leave>", lambda e: self.np_canvas.unbind_all("<MouseWheel>"))
        self._np_vcmd = (self.app.root.register(urls_entry_ok), "%P")
        self.label(parent, NP_CAPTION, 8, MUTED, wraplength=self.px(1100)).pack(anchor="w", pady=(4, 0))

    def _on_np_wheel(self, event):
        try:
            self.np_canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        except Exception:
            pass

    def _build_alloc_section(self, parent):
        """The old offer-allocation table + Spread evenly / By pool size, in a
        section that is collapsed by default."""
        tk = self.tk
        self.alloc_toggle = tk.Button(parent, text="", command=self.toggle_alloc, bg=BG, fg=FG,
                                      activebackground=BG, activeforeground=FG, relief="flat", bd=0,
                                      font=(FONT, 9, "bold"), anchor="w", cursor="hand2")
        self.alloc_toggle.pack(fill="x", pady=(8, 0))
        self.alloc_body = tk.Frame(parent, bg=BG)  # packed only while open
        helpers = tk.Frame(self.alloc_body, bg=BG)
        helpers.pack(fill="x", pady=(4, 0))
        self.button(helpers, "Spread evenly", self.on_spread_evenly).pack(side="left")
        self.button(helpers, "By pool size", self.on_by_pool).pack(side="left", padx=(6, 0))
        self.label(helpers, "Changes the selected day's offer allocations; Save in the plan editor above.",
                   8, MUTED).pack(side="left", padx=(10, 0))
        self._build_table(self.alloc_body)
        self._update_alloc_toggle()
        if self.alloc_open:
            self.alloc_body.pack(fill="both", expand=True, after=self.alloc_toggle)

    def _update_alloc_toggle(self):
        self.alloc_toggle.configure(text=("▾ " if self.alloc_open else "▸ ") + ALLOC_SECTION_TITLE)

    def toggle_alloc(self):
        self.alloc_open = not self.alloc_open
        if self.alloc_open:
            self.alloc_body.pack(fill="both", expand=True, after=self.alloc_toggle)
        else:
            self._close_editor()
            self.alloc_body.pack_forget()
        self._update_alloc_toggle()

    def _build_table(self, parent):
        tk, ttk = self.tk, self.ttk
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="browse", height=8)
        for key, heading, width in COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=self.px(width), anchor="w" if key in ("brand", "category") else "e",
                             stretch=True)
        self.tree.tag_configure("readonly", foreground=DIM)
        self.tree.tag_configure("odd", background="#2a2a28")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", lambda e: self.edit_selected())
        self.label(parent, "Double-click an Allocation cell to edit it. Pool = crawled + deferred + "
                           "discovered; days of supply = pool ÷ allocation.", 8, MUTED).pack(anchor="w", pady=(4, 0))

    def _build_editor(self, parent):
        tk, ttk = self.tk, self.ttk
        f = tk.Frame(parent, bg=CARD, padx=10, pady=8)
        f.pack(fill="x", pady=(8, 0))
        self.label(f, "Plan for", 9, MUTED).grid(row=0, column=0, sticky="w")
        self.day_combo = ttk.Combobox(f, textvariable=self.day_var, state="readonly", width=22,
                                      values=[day_label(k, self.today) for k in day_keys(self.today)])
        self.day_combo.grid(row=0, column=1, padx=(6, 14))
        self.day_combo.bind("<<ComboboxSelected>>", self.on_day_selected)
        self.label(f, "Daily total", 9, MUTED).grid(row=0, column=2, sticky="w")
        self.total_spin = tk.Spinbox(f, from_=0, to=daily_plan.MAX_TOTAL, increment=50, width=7,
                                     textvariable=self.total_var, command=self.on_total_changed,
                                     bg=BG, fg=FG, insertbackground=FG, buttonbackground=LINE,
                                     relief="flat", font=(FONT, 10))
        self.total_spin.grid(row=0, column=3, padx=(6, 14))
        self.total_spin.bind("<KeyRelease>", lambda e: self.on_total_changed())
        tk.Checkbutton(f, text="Fill remainder in priority order", variable=self.fill_var,
                       command=self.on_fill_changed, bg=CARD, fg=FG, selectcolor=BG,
                       activebackground=CARD, activeforeground=FG, font=(FONT, 9)).grid(row=0, column=4, padx=(0, 14))
        self.sum_label = self.label(f, "", 10, FG, True)
        self.sum_label.grid(row=0, column=7, sticky="w")
        f.columnconfigure(8, weight=1)
        self.clear_btn = self.button(f, "Clear override", self.on_clear_override)
        self.clear_btn.grid(row=0, column=9, padx=(6, 6))
        self.save_btn = self.button(f, "Save", self.on_save, accent=True)
        self.save_btn.grid(row=0, column=10)
        self.override_label = self.label(f, "", 8, MUTED)
        self.override_label.grid(row=1, column=0, columnspan=11, sticky="w", pady=(4, 0))
        self.day_var.set(day_label(self.day_key, self.today))

    def _build_actions(self, parent):
        tk = self.tk
        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x", pady=(10, 0))
        self.refresh_btn = self.button(f, "Refresh numbers", self.on_refresh)
        self.refresh_btn.pack(side="left")
        self.walk_btn = self.button(f, "Full site walk (slow)", self.on_walk)
        self.walk_btn.pack(side="left", padx=(6, 0))
        self.preview_btn = self.button(f, "Preview today's split", self.on_preview)
        self.preview_btn.pack(side="left", padx=(6, 0))
        self.label(f, "Uploads happen in the scheduled runs only.", 8, MUTED).pack(side="right")
        st = tk.Frame(parent, bg=BG)
        st.pack(fill="x", pady=(6, 0))
        tk.Label(st, textvariable=self.status_var, bg=BG, fg=MUTED, font=(FONT, 8), anchor="w").pack(fill="x")
        tk.Label(st, textvariable=self.walk_var, bg=BG, fg=MUTED, font=(FONT, 8), anchor="w").pack(fill="x")

    # ---------- data ----------
    def load_local(self):
        """Re-reads every cached JSON file (cheap, local) and redraws."""
        self._roll_day()
        self.inventory = load_json(HERE / INVENTORY_FILE, {}) or {}
        self.hm_inventory = load_json(HERE / HM_INVENTORY_FILE, None)
        self.catalog = load_json(HERE / CATALOG_STATUS_FILE, {}) or {}
        self.deferred = deferred_count(load_json(HERE / DEFERRED_FILE, []))
        self.new_items = load_json(HERE / NEW_ITEMS_LOG, None)
        self.created = created_from_history(self.inventory, self.today)
        if self.created_by_cat is not None:
            self.created = merge_created_today(self.created, self.created_by_cat)
        if np_is_dirty(self.np_plan, self.np_saved):
            self.status_var.set(f"Kept your unsaved URL targets ({NP_PLAN_FILE} not reloaded).")
        else:
            self.np_plan = new_product_plan.load_plan(str(HERE / NP_PLAN_FILE))
            self.np_saved = copy.deepcopy(self.np_plan)
        self.render()
        self._qp_render()

    def _roll_day(self):
        """The app can stay open past midnight UTC (03:00 Moscow, when the new-products job
        runs): move 'today' forward so 'Created today' counts the new day. True if it moved."""
        now = daily_plan.utc_today()
        if now == self.today:
            return False
        old = self.today
        self.today = now
        self.created_by_cat = None
        if not self.dirty and self.day_key <= old:
            self.day_key = first_planned_day(self.plan, now)
            self.day = get_day(self.plan, self.day_key)
        if getattr(self, "day_combo", None) is not None:
            self.day_combo.configure(values=[day_label(k, now) for k in day_keys(now)])
            self.day_var.set(day_label(self.day_key, now))
        return True

    def _watch_day(self):
        try:
            if self._roll_day():
                self.load_local()
                self.start_background_reads()
        finally:
            self.app.root.after(60_000, self._watch_day)

    def start_background_reads(self):
        def quota_work():
            q = read_quota()
            self.post(self._set_quota, q)

        def created_work():
            try:
                _, by_cat = daily_plan.created_today(str(HERE / NEW_ITEMS_LOG), self.today,
                                                     str(HERE / daily_plan.PRODUCTS_CSV))
            except Exception as e:
                print(f"created_today failed: {e}")
                return
            self.post(self._set_created, by_cat)

        threading.Thread(target=quota_work, daemon=True).start()
        threading.Thread(target=created_work, daemon=True).start()

    def _set_quota(self, q):
        self.quota = q
        self.quota_state = "ok" if q else "failed"
        self.render_cards()
        self._qp_update()

    def _set_created(self, by_cat):
        self.created_by_cat = by_cat
        self.created = merge_created_today(created_from_history(self.inventory, self.today), by_cat)
        self.render_table()

    # ---------- drawing ----------
    def render(self):
        self.render_cards()
        self.render_np_table()
        self.render_table()
        self.render_editor()

    # ---------- New Product Adding table ----------
    def render_np_table(self):
        """Rebuilds the grid from new_product_plan.build_rows (sorted by priority)."""
        self._np_render_pending = False
        tk, ttk = self.tk, self.ttk
        for w in self.np_grid.winfo_children():
            w.destroy()
        self.np_vars = {}
        self.np_rows = new_product_plan.build_rows(self.inventory, self.np_plan, self.hm_inventory)
        g = self.np_grid
        planned_head = f"Planned offers ({day_label(self.day_key, self.today).split('  ')[-1]})"
        for col, (key, heading) in enumerate(NP_COLUMNS):
            self.label(g, planned_head if key == "planned" else heading, 9, MUTED, True).grid(
                row=0, column=col, sticky="w", padx=(0, 14), pady=(0, 4))
        g.columnconfigure(len(NP_COLUMNS), weight=1)
        for i, row in enumerate(self.np_rows, start=1):
            editable = bool(row.get("editable"))
            color = FG if editable else DIM
            brand, cat = row["brand"], row["category"]
            self.label(g, brand, 9, color).grid(row=i, column=0, sticky="w", padx=(0, 14), pady=2)
            self.label(g, cat, 9, color).grid(row=i, column=1, sticky="w", padx=(0, 14), pady=2)
            pvar = tk.StringVar(value=row.get("priority_label") or DASH)
            combo = ttk.Combobox(g, textvariable=pvar, state="readonly" if editable else "disabled",
                                 values=list(new_product_plan.PRIORITY_LABELS), width=10)
            combo.grid(row=i, column=2, sticky="w", padx=(0, 14), pady=2)
            # the wheel scrolls the table; it must never cycle a priority by accident
            combo.bind("<MouseWheel>", lambda e: (self._on_np_wheel(e), "break")[1])
            if editable:
                combo.bind("<<ComboboxSelected>>",
                           lambda e, b=brand, c=cat, v=pvar: self.on_np_priority(b, c, v.get()))
            self.label(g, np_fmt(row.get("total_urls")), 9, color, anchor="e").grid(
                row=i, column=3, sticky="e", padx=(0, 14), pady=2)
            self.label(g, np_fmt(row.get("eligible_urls")), 9, color, anchor="e").grid(
                row=i, column=4, sticky="e", padx=(0, 14), pady=2)
            n_planned = planned_offers(self.day, brand, cat)
            self.label(g, f"{n_planned:,}" if n_planned else DASH, 9, FG if n_planned else DIM,
                       bold=bool(n_planned), anchor="e").grid(row=i, column=5, sticky="e", padx=(0, 14), pady=2)
            uvar = tk.StringVar(value=str(row["urls_to_add"]) if editable else DASH)
            ent = tk.Entry(g, textvariable=uvar, width=8, justify="right", bg=BG, fg=FG,
                           insertbackground=FG, relief="flat", font=(FONT, 9), disabledbackground=CARD,
                           disabledforeground=DIM)
            ent.grid(row=i, column=6, sticky="w", padx=(0, 14), pady=2)
            btns = tk.Frame(g, bg=CARD)
            btns.grid(row=i, column=7, sticky="w", pady=2)
            all_btn = self.button(btns, "Add all", lambda b=brand, c=cat: self.on_np_fill(b, c, "all"))
            half_btn = self.button(btns, "Add half", lambda b=brand, c=cat: self.on_np_fill(b, c, "half"))
            all_btn.pack(side="left")
            half_btn.pack(side="left", padx=(6, 0))
            if editable:
                ent.configure(validate="key", validatecommand=self._np_vcmd)
                ent.bind("<Return>", lambda e, b=brand, c=cat: self.on_np_urls_commit(b, c))
                ent.bind("<FocusOut>", lambda e, b=brand, c=cat: self.on_np_urls_commit(b, c))
                ent.bind("<KeyRelease>", lambda e, b=brand, c=cat: self.on_np_urls_live(b, c))
                ent.bind("<Escape>", lambda e, b=brand, c=cat: self._np_revert(b, c, None))
                self.np_vars[(brand, cat)] = (uvar, ent, row.get("eligible_urls") or 0)
                self._np_color(brand, cat)
            else:
                ent.configure(state="disabled")
                all_btn.configure(state="disabled")
                half_btn.configure(state="disabled")
            if row.get("note"):
                self.label(g, row["note"], 8, MUTED).grid(row=i, column=8, sticky="w", padx=(6, 0), pady=2)
        self.render_np_state()

    def render_np_state(self):
        errors, note = np_save_state(self.np_plan, self.np_saved)
        self.np_save_btn.configure(state="disabled" if errors else "normal")
        self.np_note.configure(text=note, fg=STATUS["critical"] if errors else
                               (STATUS["warning"] if note else MUTED))

    def _np_color(self, brand, cat):
        item = self.np_vars.get((brand, cat))
        if not item:
            return
        var, ent, eligible = item
        try:
            over = parse_urls_to_add(var.get()) > eligible
        except ValueError:
            over = False
        ent.configure(fg=STATUS["warning"] if over else FG)

    def _np_revert(self, brand, cat, message):
        item = self.np_vars.get((brand, cat))
        if item:
            item[0].set(str(new_product_plan.get_entry(self.np_plan, brand, cat)["urls_to_add"]))
            self._np_color(brand, cat)
        if message:
            self.status_var.set(message)

    def _schedule_np_render(self):
        """Re-sort after the current event finishes (never destroy the widget
        whose event handler is still running)."""
        if not self._np_render_pending:
            self._np_render_pending = True
            self.app.root.after_idle(self.render_np_table)

    def on_np_priority(self, brand, cat, label):
        prio = priority_from_label(label)
        try:
            self.np_plan = new_product_plan.set_entry(self.np_plan, brand, cat, priority=prio)
        except ValueError as err:
            self.status_var.set(f"Priority not changed: {err}")
            self._schedule_np_render()
            return
        self.status_var.set(f"{brand} {cat}: priority {label}. Rows re-sorted.")
        self._schedule_np_render()

    def _np_set_urls(self, brand, cat, n):
        try:
            self.np_plan = new_product_plan.set_entry(self.np_plan, brand, cat, urls_to_add=n)
        except ValueError as err:
            self._np_revert(brand, cat, f"URLs to add not changed: {err}")
            return
        self._np_color(brand, cat)
        self.render_np_state()

    def on_np_urls_live(self, brand, cat):
        """While typing: apply a parseable value at once, so the warning colour
        and the unsaved-changes note are live. Never reverts mid-typing (an
        empty or invalid value is left for the commit to revert)."""
        item = self.np_vars.get((brand, cat))
        if not item:
            return
        try:
            n = parse_urls_to_add(item[0].get())
            if n != new_product_plan.get_entry(self.np_plan, brand, cat)["urls_to_add"]:
                self.np_plan = new_product_plan.set_entry(self.np_plan, brand, cat, urls_to_add=n)
        except ValueError:
            pass
        self._np_color(brand, cat)
        self.render_np_state()

    def on_np_urls_commit(self, brand, cat):
        """Commits the entry's text. Returns False if it was invalid and reverted."""
        item = self.np_vars.get((brand, cat))
        if not item:
            return True
        text = item[0].get()
        try:
            n = parse_urls_to_add(text)
        except ValueError as err:
            self._np_revert(brand, cat, f"{brand} {cat}: {err}; reverted.")
            return False
        if n != new_product_plan.get_entry(self.np_plan, brand, cat)["urls_to_add"]:
            self._np_set_urls(brand, cat, n)
            if new_product_plan.get_entry(self.np_plan, brand, cat)["urls_to_add"] != n:
                return False  # _np_set_urls refused and reverted
        item[0].set(str(n))  # normalises e.g. '007' -> '7'
        return True

    def on_np_fill(self, brand, cat, mode):
        row = next((r for r in self.np_rows if r["brand"] == brand and r["category"] == cat), None)
        item = self.np_vars.get((brand, cat))
        if row is None or item is None:
            return
        n = new_product_plan.add_all(row) if mode == "all" else new_product_plan.add_half(row)
        item[0].set(str(n))
        self._np_set_urls(brand, cat, n)

    def on_np_save(self):
        # tk.Button never takes focus, so a typed value may not be committed yet
        ok = True
        for brand, cat in list(self.np_vars):
            if not self.on_np_urls_commit(brand, cat):
                ok = False
        if not ok:
            self.render_np_state()
            self.status_var.set("Not saved: an invalid 'URLs to add' value was reverted; check and save again.")
            return
        try:
            saved = new_product_plan.save_plan(self.np_plan, str(HERE / NP_PLAN_FILE))
        except (ValueError, OSError) as err:
            self.status_var.set(f"Not saved: {err}")
            self.render_np_state()
            return
        self.np_plan = saved
        self.np_saved = copy.deepcopy(saved)
        self.status_var.set(f"Saved {NP_PLAN_FILE} ({datetime.now():%H:%M:%S}).")
        self.render_np_state()

    # ---------- Add a new website (local file writes only; UI thread) ----------
    def open_website_form(self):
        tk = self.tk
        if self.website_win is not None:
            try:
                self.website_win.lift()
                self.website_win.focus_set()
                return
            except Exception:
                self.website_win = None
        win = tk.Toplevel(self.app.root)
        self.website_win = win
        win.title("Add a new website")
        win.configure(bg=BG, padx=self.px(14), pady=self.px(12))
        win.transient(self.app.root)
        win.resizable(True, False)
        self.label(win, "Add a new website", 12, FG, True).pack(anchor="w")
        self.label(win, "Records a request and writes an EPIC under '## Todo' in TASKS.md. "
                        "The website is not fetched.", 8, MUTED, wraplength=self.px(480)).pack(anchor="w", pady=(2, 8))
        self.label(win, "Website URL", 9, MUTED).pack(anchor="w")
        url_var = tk.StringVar()
        url_ent = tk.Entry(win, textvariable=url_var, width=60, bg=CARD, fg=FG, insertbackground=FG,
                           relief="flat", font=(FONT, 10))
        url_ent.pack(fill="x", pady=(2, 8))
        self.label(win, f"Description (what to sell from it, up to {new_sources.MAX_DESCRIPTION} characters)",
                   9, MUTED).pack(anchor="w")
        desc = tk.Text(win, height=6, width=60, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                       font=(FONT, 10), wrap="word")
        desc.pack(fill="x", pady=(2, 8))
        err = self.label(win, "", 9, STATUS["critical"], wraplength=self.px(480))
        err.pack(anchor="w")
        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", pady=(8, 0))

        def close():
            self.website_win = None
            win.destroy()

        def create():
            try:
                rec = new_sources.create_request(
                    url_var.get(), desc.get("1.0", "end-1c"),
                    requests_path=str(HERE / new_sources.REQUESTS_FILE),
                    tasks_path=str(HERE / new_sources.TASKS_FILE))
            except (ValueError, OSError) as e:
                err.configure(text=str(e))
                return
            close()
            self.status_var.set(f"Epic created for {rec['url']} (request #{rec['id']}) in "
                                f"{new_sources.TASKS_FILE}; saved to {new_sources.REQUESTS_FILE}.")

        self.button(btns, "Cancel", close).pack(side="right")
        self.button(btns, "Create epic", create, accent=True).pack(side="right", padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", close)
        url_ent.focus_set()

    def render_cards(self):
        for w in self.cards.winfo_children():
            w.destroy()
        value, sub = quota_text(self.quota)
        if self.quota_state == "checking":
            sub = "checking Ozon…"
        elif self.quota_state == "failed":
            cached = self.inventory.get("quota") if isinstance(self.inventory, dict) else None
            if isinstance(cached, dict) and isinstance(cached.get("usage"), int):
                sub = f"offline · last seen {cached['usage']:,}/{cached.get('limit')} at {self.inventory.get('built', '?')}"
        self.card(0, "Ozon daily_create quota", value, sub,
                  STATUS["warning"] if value == "unknown" and self.quota_state == "failed" else MUTED)
        today_plan = daily_plan.plan_for_date(self.plan, self.today)
        made = created_today_total(self.new_items, self.today)
        over = made > today_plan["total"]
        self.card(1, "Today's plan", f"{made:,} / {today_plan['total']:,}",
                  f"created / planned · {self.today} UTC" + (" · over plan" if over else ""),
                  STATUS["critical"] if over else MUTED)
        self.card(2, "Deferred queue", f"{self.deferred:,}", "offers waiting in deferred_items.json")
        col = 3
        for brand, val, sub in brand_cards(self.catalog):
            self.card(col, brand, val, sub)
            col += 1
        built = self.catalog.get("built") if isinstance(self.catalog, dict) else None
        inv_built = self.inventory.get("built") if isinstance(self.inventory, dict) else None
        self.built_label.configure(text=f"catalog_status built {built or '—'} · inventory built {inv_built or '—'}")

    def render_table(self):
        self._close_editor()
        sel = self.tree.selection()
        hm_created = {lbl: n for (b, lbl), n in (self.created_by_cat or {}).items() if b == HM}
        self.rows = build_rows(self.inventory, self.catalog, self.day, self.created,
                               self.hm_inventory, hm_created)
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            tags = ("readonly",) if not is_editable(row) else (("odd",) if i % 2 else ())
            self.tree.insert("", "end", iid=str(i), values=[fmt_cell(k, row) for k, _, _ in COLUMNS], tags=tags)
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def render_editor(self):
        s = allocation_sum(self.day)
        total = self.day["total"]
        reserve = daily_plan.reserve_for(total)
        self.sum_label.configure(text=f"Allocated {s:,} / {total:,}"
                                      + (f"  ·  +{reserve:,} reserve (15%, only to complete the target)"
                                         if reserve else ""),
                                 fg=STATUS["critical"] if s > total else FG)
        errors = plan_errors(set_day(self.plan, self.day_key, self.day))
        self.save_btn.configure(state="disabled" if errors else "normal")
        self.clear_btn.configure(state="normal" if has_override(self.plan, self.day_key) else "disabled")
        if self.day_key == DEFAULT_KEY:
            note = "Editing the default plan (applies to every day without an override)."
        elif has_override(self.plan, self.day_key):
            note = f"Editing the override for {self.day_key}."
        else:
            note = f"{self.day_key} has no override yet -- showing the default; Save creates one."
        if self.dirty:
            note += "  Unsaved changes."
        if errors:
            note += "  Can't save: " + "; ".join(errors)
        self.override_label.configure(text=note, fg=STATUS["critical"] if errors else MUTED)

    # ---------- editing ----------
    def _changed(self):
        self.dirty = True
        self.render_table()
        self.render_editor()
        self._schedule_np_render()  # keep 'Planned offers' in step with the allocation table

    def on_day_selected(self, _e=None):
        idx = self.day_combo.current()
        key = day_keys(self.today)[idx] if idx >= 0 else DEFAULT_KEY
        if key == self.day_key:
            return
        if self.dirty:
            from tkinter import messagebox
            if not messagebox.askyesno("Unsaved changes", "Discard unsaved changes to this day?",
                                       parent=self.app.root):
                self.day_var.set(day_label(self.day_key, self.today))
                return
        self.day_key = key
        self.day = get_day(self.plan, key)
        self.dirty = False
        self.total_var.set(str(self.day["total"]))
        self.fill_var.set(self.day["fill_remainder"])
        self.render_table()
        self.render_editor()
        self._schedule_np_render()

    def on_total_changed(self):
        try:
            n = int(self.total_var.get())
        except ValueError:
            return
        n = max(0, min(daily_plan.MAX_TOTAL, n))
        if n != self.day["total"]:
            self.day["total"] = n
            self._changed()

    def on_fill_changed(self):
        self.day["fill_remainder"] = bool(self.fill_var.get())
        self._changed()

    def _ms_pools(self):
        return {(r["brand"], r["category"]): r["pool"] for r in self.rows if is_editable(r)}

    def on_spread_evenly(self):
        pools = self._ms_pools()
        labels = [lbl for lbl, p in pools.items() if p] or list(pools)
        allocs = {lbl: 0 for lbl in pools}
        allocs.update(spread_evenly(self.day["total"], labels))
        self.day = apply_helper(self.day, allocs)
        self._changed()

    def on_by_pool(self):
        self.day = apply_helper(self.day, by_pool_size(self.day["total"], self._ms_pools()))
        self._changed()

    def on_save(self):
        new_plan = set_day(self.plan, self.day_key, self.day)
        saved, errors = save_plan_from_ui(new_plan, str(HERE / PLAN_FILE))
        if errors:
            self.status_var.set("Not saved: " + "; ".join(errors))
            self.render_editor()
            return
        self.plan = saved
        self.day = get_day(self.plan, self.day_key)
        self.dirty = False
        self.status_var.set(f"Saved {PLAN_FILE} ({datetime.now():%H:%M:%S}).")
        self.render()
        self._qp_render()

    def on_clear_override(self):
        if not has_override(self.plan, self.day_key):
            return
        new_plan = clear_override(self.plan, self.day_key)
        saved, errors = save_plan_from_ui(new_plan, str(HERE / PLAN_FILE))
        if errors:
            self.status_var.set("Not saved: " + "; ".join(errors))
            return
        self.plan = saved
        self.day = get_day(self.plan, self.day_key)
        self.dirty = False
        self.total_var.set(str(self.day["total"]))
        self.fill_var.set(self.day["fill_remainder"])
        self.status_var.set(f"Cleared the override for {self.day_key}.")
        self.render()

    def on_double_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        iid = self.tree.identify_row(e.y)
        col = self.tree.identify_column(e.x)
        alloc_col = f"#{[c[0] for c in COLUMNS].index('allocation') + 1}"
        if iid and col == alloc_col:
            self.edit_cell(iid)

    def edit_selected(self):
        sel = self.tree.selection()
        if sel:
            self.edit_cell(sel[0])

    def edit_cell(self, iid):
        row = self.rows[int(iid)]
        if not is_editable(row):
            self.status_var.set(f"{row['brand']}: {HM_NOTE} -- allocation can't be edited.")
            return
        self._close_editor()
        bbox = self.tree.bbox(iid, "allocation")
        if not bbox:
            return
        x, y, w, h = bbox
        var = self.tk.StringVar(value=str(row["allocation"] or 0))
        ent = self.tk.Entry(self.tree, textvariable=var, justify="right", bg=BG, fg=FG,
                            insertbackground=FG, relief="flat", font=(FONT, 9))
        ent.place(x=x, y=y, width=w, height=h)
        ent.select_range(0, "end")
        ent.focus_set()

        def commit(_e=None):
            try:
                n = int(var.get().strip() or 0)
                self.day = set_allocation(self.day, row["brand"], row["category"], n)
            except ValueError as err:
                self.status_var.set(f"Allocation not changed: {err}")
                self._close_editor()
                return
            self._close_editor()
            self._changed()

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda _e: self._close_editor())
        self.editor = ent

    def _close_editor(self):
        if self.editor is not None:
            ed, self.editor = self.editor, None
            try:
                ed.destroy()
            except Exception:
                pass

    # ---------- actions (all subprocesses in threads) ----------
    def _run_bg(self, name, actions, on_done):
        if name in self.busy:
            return
        self.busy.add(name)
        self._update_buttons()

        def work():
            results = [(a, *run_script(a)) for a in actions]
            self.post(self._bg_done, name, results, on_done)

        threading.Thread(target=work, daemon=True).start()

    def _bg_done(self, name, results, on_done):
        self.busy.discard(name)
        self._update_buttons()
        on_done(results)

    def _update_buttons(self):
        for btn, name in ((self.refresh_btn, "refresh"), (self.walk_btn, "walk"), (self.preview_btn, "preview")):
            btn.configure(state="disabled" if name in self.busy else "normal")

    @staticmethod
    def _summary(results):
        parts = []
        for action, rc, out in results:
            ok = "ok" if rc == 0 else ("failed" if rc is not None else "not run")
            parts.append(f"{' '.join(COMMANDS[action])}: {ok}" + ("" if rc == 0 else f" ({tail(out, 1)})"))
        return " · ".join(parts)

    def on_refresh(self):
        self.status_var.set(f"Refreshing numbers (catalog_status.py, category_inventory.py) since "
                            f"{datetime.now():%H:%M}…")

        def done(results):
            self.status_var.set(f"Refreshed {datetime.now():%H:%M}: " + self._summary(results))
            self.load_local()
            self.quota_state = "checking"
            self.render_cards()
            self.start_background_reads()

        self._run_bg("refresh", ["catalog_status", "inventory"], done)

    def on_walk(self):
        from tkinter import messagebox
        if not messagebox.askokcancel(
                "Full site walk",
                "This fetches every product page in every supported M&S category and can take "
                "hours. It only counts -- nothing is queued or uploaded. Start it?",
                parent=self.app.root):
            return
        self.walk_var.set(f"Full site walk running since {datetime.now():%H:%M} "
                          "(category_inventory.py --walk)…")

        def done(results):
            self.walk_var.set(f"Full site walk finished {datetime.now():%H:%M}: " + self._summary(results))
            self.load_local()

        self._run_bg("walk", ["walk"], done)

    def on_preview(self):
        self.status_var.set(f"Running upload_to_ozon.py --dry-run since {datetime.now():%H:%M} "
                            "(nothing is submitted)…")

        def done(results):
            _, rc, out = results[0]
            self.status_var.set(f"Preview finished {datetime.now():%H:%M} (exit {rc}).")
            self.show_text("Preview today's split (dry run)", out)

        self._run_bg("preview", ["preview"], done)

    def show_text(self, title, text):
        tk = self.tk
        win = tk.Toplevel(self.app.root)
        win.title(title)
        win.configure(bg=BG)
        win.geometry("1000x600")
        txt = tk.Text(win, bg=CARD, fg=FG, insertbackground=FG, font=("Consolas", 9), relief="flat",
                      wrap="none", padx=8, pady=6)
        sb = self.ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", text or "(no output)")
        txt.see("end")
        txt.configure(state="disabled")


NewProductAddingTab = PrioritizationTab  # the tab's current name


register_tab(TAB_OVERVIEW, OverviewTab)
register_tab(TAB_DAILY, DailyTasksTab)
register_tab(TAB_PRIORITIZATION, PrioritizationTab)


class OzonSalesApp(_WidgetHelpers):
    """The window: one ttk.Notebook with a tab per TAB_BUILDERS entry. Owns
    the single worker-thread -> UI-thread queue; tabs hand results back via
    app.post and never touch Tk from a thread."""

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self._results = queue.Queue()  # worker threads -> UI thread
        self.tabs = {}

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        self.dpi = max(1.0, self.root.winfo_fpixels("1i") / 96.0)  # DPI-aware: scale pixel sizes
        self.root.geometry(f"{self.px(1180)}x{self.px(780)}")
        self.root.minsize(self.px(980), self.px(640))
        self._style()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        frames = {}
        for name, builder in TAB_BUILDERS.items():
            frame = tk.Frame(self.notebook, bg=BG, padx=16, pady=12)
            self.notebook.add(frame, text=name)
            frames[name] = frame
            try:
                self.tabs[name] = builder(self, frame)
            except Exception as e:
                import traceback
                print(f"Tab {name!r} failed to build: {e}\n{traceback.format_exc()}")
                for w in frame.winfo_children():
                    w.destroy()
                self.label(frame, f"{name} could not be loaded: {e}", 10, STATUS["critical"],
                           wraplength=self.px(900)).pack(anchor="w")
                self.tabs[name] = None
        if DEFAULT_TAB in frames:
            self.notebook.select(frames[DEFAULT_TAB])

        self.root.after(POLL_MS, self._drain_results)

    # ---------- thread hand-off ----------
    def post(self, fn, *args):
        """Called from worker threads: queue fn(*args) for the UI thread
        (tkinter itself must only be touched from the UI thread)."""
        self._results.put((fn, args))

    def _drain_results(self):
        try:
            while True:
                fn, args = self._results.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    print(f"UI update failed: {e}")
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain_results)

    def _style(self):
        s = self.ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                    bordercolor=LINE, rowheight=self.px(24), font=(FONT, 9))
        s.configure("Treeview.Heading", background=LINE, foreground=FG, font=(FONT, 9, "bold"),
                    bordercolor=LINE, relief="flat")
        s.map("Treeview", background=[("selected", "#3a4a66")], foreground=[("selected", FG)])
        s.map("Treeview.Heading", background=[("active", "#4a4a46")])
        s.configure("TCombobox", fieldbackground=CARD, background=LINE, foreground=FG, arrowcolor=FG)
        s.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", FG)],
              background=[("readonly", LINE)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        s.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG, arrowcolor=FG, bordercolor=BG)
        s.configure("TNotebook", background=BG, bordercolor=LINE, tabmargins=(0, 4, 0, 0))
        s.configure("TNotebook.Tab", background=LINE, foreground=FG, bordercolor=LINE,
                    lightcolor=LINE, padding=(self.px(14), self.px(5)), font=(FONT, 9))
        s.map("TNotebook.Tab", background=[("selected", CARD), ("active", "#4a4a46")],
              foreground=[("selected", FG)], lightcolor=[("selected", CARD)])

    def run(self):
        self.root.mainloop()


PlannerApp = OzonSalesApp  # backward-compatible alias


def main():
    try:  # crisp text on scaled displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    os.chdir(HERE)
    OzonSalesApp().run()


if __name__ == "__main__":
    main()
