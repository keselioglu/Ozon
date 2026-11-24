"""
Ozon Sales -- tabbed desktop app (one ttk.Notebook window).

Tabs, in order:
  * Overview -- placeholder ("Coming soon")
  * Daily tasks -- view-only status (DailyTasksTab): scheduled MandsOzon* /
    OzonSales_* tasks (next run, last run, coloured result), the latest
    daily_run card with its per-step list, a stale banner when
    daily_run_status.json isn't today's, and collect errors. Data comes from
    task_status.collect_status() on a worker thread; Refresh (the only
    button) or the 60s auto-refresh, never two collects at once. A
    recheck_stockouts timeout is shown as a warning, not a failure.
  * Prioritization (opens first) -- the daily planner: splits the day's
    new-offer plan (daily_plan.json) by brand x category, with the numbers
    to decide on.

Later tasks add or replace tabs through the module-level hook
register_tab(name, builder), usable as a plain call or as a decorator;
builder(app, parent) gets the OzonSalesApp shell (root, tk, ttk, post, px,
label, button) and a tk.Frame already added to the notebook. Tabs must hand
worker-thread results back through app.post (the shell's single queue) and
never touch Tk from a thread or start their own poll loop.

Prioritization tab:
  * header: Ozon daily_create quota (live read of /v4/product/info/limit,
    'unknown' if it fails), today's plan created/planned
    (new_items_submitted.json), deferred queue size, and per-brand catalog
    numbers from catalog_status.json
  * table: one row per brand x category from category_inventory.json, with
    the selected day's allocation (double-click to edit; H&M rows are
    read-only -- no add pipeline yet)
  * plan editor: Default / Today / Today+1..+7 overrides, total, fill
    remainder, Spread evenly / By pool size, Save (daily_plan.save_plan,
    blocked while daily_plan.validate_plan reports errors), Clear override
  * actions: Refresh numbers, Full site walk (slow), Preview today's split
    (upload_to_ozon.py --dry-run)

The app is still READ-ONLY toward Ozon: the only Ozon call is the quota read, and the only
scripts it starts are the read-only snapshots plus the upload's --dry-run.
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
from datetime import date as _date, datetime, timedelta
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
import task_status  # noqa: E402  (read-only: local JSON + a scheduler query, never changes a task)

INVENTORY_FILE = "category_inventory.json"
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
# catalog_status.py / category_inventory.py are read-only snapshots and the
# upload only runs with --dry-run (exits before /v3/product/import).
COMMANDS = {
    "catalog_status": ["catalog_status.py"],
    "inventory": ["category_inventory.py"],
    "walk": ["category_inventory.py", "--walk"],
    "preview": ["upload_to_ozon.py", "--dry-run"],
}
TIMEOUTS = {"catalog_status": 15 * 60, "inventory": 15 * 60, "walk": 8 * 3600, "preview": 30 * 60}

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


def build_rows(inventory, catalog_status, plan_day, created):
    """Table rows for one plan day. inventory: category_inventory.json dict
    (or None); catalog_status: catalog_status.json dict (or None); plan_day:
    {"total", "allocations", "fill_remainder"}; created: created_from_history()
    shape. M&S gets one row per daily_plan category; H&M one read-only row."""
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

    hm_status = ((catalog_status or {}).get("brands") or {}).get(HM) or {}
    hm_inv = brands.get(HM) or {}
    hm_live = hm_status.get("live", hm_inv.get("live"))
    rows.append({
        "brand": HM, "category": "(all)", "live": hm_live,
        "crawled_not_live": None, "deferred": None, "discovered_est": None,
        "created_today": None, "created_7d": None, "allocation": None,
        "pool": None, "days_of_supply": None, "editable": False, "note": HM_NOTE,
    })
    return rows


def is_editable(row):
    return bool(row and row.get("editable")) and row.get("brand") == MS


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
    """New day with allocation[brand][label] = n. Only M&S is editable."""
    if brand != MS:
        raise ValueError(f"{brand} has {HM_NOTE} -- its allocation can't be edited")
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
    """New day whose M&S allocations are replaced by `allocations`."""
    out = copy.deepcopy(day)
    out.setdefault("allocations", {})[MS] = dict(allocations)
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
            rows.append({
                "name": _text(t.get("name")) or "?",
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


def _step_is_normal_timeout(step):
    text = _fold(step.get("script")) + " " + _fold(step.get("name"))
    return any(k in text for k in NORMAL_TIMEOUT_STEPS)


def step_level(step):
    """daily_run step record -> STATUS key. Only the recheck_stockouts timeout is a warning."""
    try:
        step = _as_dict(step)
        status = _fold(step.get("status"))
        if status == "ok":
            return "good"
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


# --- Tabs (pure registration hook; no tkinter at module level) --------------------

APP_TITLE = "Ozon Sales"
TAB_OVERVIEW = "Overview"
TAB_DAILY = "Daily tasks"
TAB_PRIORITIZATION = "Prioritization"
TAB_NAMES = (TAB_OVERVIEW, TAB_DAILY, TAB_PRIORITIZATION)
DEFAULT_TAB = TAB_PRIORITIZATION  # Overview is still a placeholder
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


def _placeholder_tab(app, parent, name):
    f = app.tk.Frame(parent, bg=BG)
    f.pack(fill="both", expand=True)
    app.label(f, name, 14, bold=True).pack(anchor="w")
    app.label(f, "Coming soon", 10, MUTED).pack(anchor="w", pady=(6, 0))
    return f


def build_overview_tab(app, parent):
    return _placeholder_tab(app, parent, TAB_OVERVIEW)


JOB_COLUMNS = (("name", "Task", 260), ("next_run", "Schedule / next run", 190),
               ("last_run", "Last run", 190), ("result", "Result", 260), ("status", "Status", 110))
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

        # packed (before the jobs table) only while stale_banner() has text
        self.banner = tk.Label(outer, text="", bg=STATUS["warning"], fg=BG, font=(FONT, 9, "bold"),
                               anchor="w", padx=8, pady=4)

        self.jobs_wrap = tk.Frame(outer, bg=BG)
        self.jobs_wrap.pack(fill="both", expand=True, pady=(10, 0))
        self.jobs = self._tree(self.jobs_wrap, JOB_COLUMNS, 8)

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
            self.banner.pack(fill="x", pady=(8, 0), before=self.jobs_wrap)
        else:
            self.banner.pack_forget()

        self.jobs.delete(*self.jobs.get_children())
        for row in build_task_rows(status):
            self.jobs.insert("", "end", values=[row[k] for k, _, _ in JOB_COLUMNS], tags=(row["level"],))

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


class PrioritizationTab(_WidgetHelpers):
    """The planner: quota cards, brand x category table, plan editor, actions."""

    def __init__(self, app, parent):
        self.app = app
        self.tk, self.ttk = app.tk, app.ttk
        self.dpi = app.dpi
        tk = self.tk
        self.today = daily_plan.utc_today()
        self.plan = daily_plan.load_plan(str(HERE / PLAN_FILE))
        self.day_key = DEFAULT_KEY
        self.day = get_day(self.plan, self.day_key)
        self.dirty = False
        self.quota = None
        self.quota_state = "checking"
        self.created_by_cat = None
        self.busy = set()
        self.rows = []
        self.editor = None

        self.total_var = tk.StringVar(value=str(self.day["total"]))
        self.fill_var = tk.BooleanVar(value=self.day["fill_remainder"])
        self.day_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Loaded cached numbers.")
        self.walk_var = tk.StringVar(value="")

        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="both", expand=True)
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        self.label(head, "Prioritization", 14, bold=True).pack(side="left")
        self.built_label = self.label(head, "", 9, MUTED)
        self.built_label.pack(side="right")
        self.cards = tk.Frame(outer, bg=BG)
        self.cards.pack(fill="x", pady=(10, 0))

        self._build_table(outer)
        self._build_editor(outer)
        self._build_actions(outer)

        self.load_local()
        self.app.root.after(50, self.start_background_reads)

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

    # ---------- layout ----------
    def _build_table(self, parent):
        tk, ttk = self.tk, self.ttk
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="browse", height=15)
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
        self.button(f, "Spread evenly", self.on_spread_evenly).grid(row=0, column=5, padx=(0, 6))
        self.button(f, "By pool size", self.on_by_pool).grid(row=0, column=6, padx=(0, 14))
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
        self.inventory = load_json(HERE / INVENTORY_FILE, {}) or {}
        self.catalog = load_json(HERE / CATALOG_STATUS_FILE, {}) or {}
        self.deferred = deferred_count(load_json(HERE / DEFERRED_FILE, []))
        self.new_items = load_json(HERE / NEW_ITEMS_LOG, None)
        self.created = created_from_history(self.inventory, self.today)
        if self.created_by_cat is not None:
            self.created = merge_created_today(self.created, self.created_by_cat)
        self.render()

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

    def _set_created(self, by_cat):
        self.created_by_cat = by_cat
        self.created = merge_created_today(created_from_history(self.inventory, self.today), by_cat)
        self.render_table()

    # ---------- drawing ----------
    def render(self):
        self.render_cards()
        self.render_table()
        self.render_editor()

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
        self.rows = build_rows(self.inventory, self.catalog, self.day, self.created)
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            tags = ("readonly",) if not is_editable(row) else (("odd",) if i % 2 else ())
            self.tree.insert("", "end", iid=str(i), values=[fmt_cell(k, row) for k, _, _ in COLUMNS], tags=tags)
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def render_editor(self):
        s = allocation_sum(self.day)
        total = self.day["total"]
        self.sum_label.configure(text=f"Allocated {s:,} / {total:,}",
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
        return {r["category"]: r["pool"] for r in self.rows if is_editable(r)}

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


register_tab(TAB_OVERVIEW, build_overview_tab)
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
