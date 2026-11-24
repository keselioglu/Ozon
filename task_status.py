"""
Read-only "daily tasks" status for the UI (task #54).

Gathers three things:
  (a) Windows Task Scheduler entries for this pipeline -- found by name marker
      (TASK_NAME_MARKERS, e.g. 'MandsOzon*' and the 'OzonSales_*' tasks behind
      the run_*.bat price/stock/H&M stock launchers), never a hard-coded list.
      Only ever QUERIES the scheduler (SCHTASKS_ARGS); this module never
      creates, changes, runs or ends a task.
  (b) daily_run_status.json (written by daily_run.write_run_status) and
      whether it is from today (local date, same as daily_run.TODAY).
  (c) Optional per-job last-run files that already exist
      (hm_stock_last_run.json, catalog_status.json 'runs').

Public API: collect_status(now=None) -> dict. Never raises; problems are put
into the result as error strings. CLI: `python task_status.py` prints tables.
"""
import csv
import io
import json
import locale
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_DIR = Path(__file__).resolve().parent
RUN_STATUS_NAME = "daily_run_status.json"
HM_LAST_RUN_NAME = "hm_stock_last_run.json"
CATALOG_STATUS_NAME = "catalog_status.json"
RUN_STATUS_FILE = REPO_DIR / RUN_STATUS_NAME
HM_LAST_RUN_FILE = REPO_DIR / HM_LAST_RUN_NAME
CATALOG_STATUS_FILE = REPO_DIR / CATALOG_STATUS_NAME

SCHTASKS_TIMEOUT = 30
# Case-insensitive substrings of the task's short name. The price / stock /
# H&M stock tasks behind run_*.bat are named OzonSales_Daily*, not MandsOzon*.
TASK_NAME_MARKERS = ("MandsOzon", "OzonSales_")
# The ONLY scheduler invocation in this module: a read-only query.
SCHTASKS_ARGS = ["schtasks", "/query", "/fo", "CSV", "/v"]

KNOWN_STATES = ("running", "completed", "failed", "blocked")

# Column key -> accepted header names (English first, then localized aliases).
COLUMN_ALIASES = {
    "task_name": ("TaskName", "Görev Adı", "Görev adı", "Görev Adi"),
    "next_run": ("Next Run Time", "Sonraki Çalışma Zamanı", "Sonraki Çalıştırma Zamanı"),
    "status": ("Status", "Durum"),
    "last_run": ("Last Run Time", "Son Çalışma Zamanı", "Son Çalıştırma Zamanı"),
    "last_result": ("Last Result", "Son Sonuç"),
    "action": ("Task To Run", "Çalıştırılacak Görev"),
    "state": ("Scheduled Task State", "Zamanlanmış Görev Durumu"),
    "start_time": ("Start Time", "Başlangıç Saati"),
    "schedule_type": ("Schedule Type", "Zamanlama Türü"),
}
# Fixed English `/v` column order, used when a header name isn't recognised.
POSITIONAL_FALLBACK = {
    "task_name": 1,
    "next_run": 2,
    "status": 3,
    "last_run": 5,
    "last_result": 6,
    "action": 8,
}

NULL_VALUES = {"", "n/a", "yok", "none", "never"}

RESULT_CODES = {
    0: ("Success", True),
    0x41300: ("Ready", None),
    0x41301: ("Running", None),
    0x41302: ("Disabled", None),
    0x41303: ("Never run", None),
    0x41304: ("No more runs scheduled", None),
    0x41306: ("Terminated by user", False),
    0x8004131F: ("An instance is already running", False),
    0x800710E0: ("Refused (operator/admin)", False),
    0x80070002: ("File not found", False),
    2: ("File not found", False),
    1: ("Error (exit code 1)", False),
}


# --------------------------------------------------------------------------
# (a) Scheduled tasks
# --------------------------------------------------------------------------

def _clean(value, is_time=False):
    """'N/A' / 'Yok' / '' (and, for times, never-run 1999 placeholders) -> None."""
    if value is None:
        return None
    v = str(value).strip()
    if v.lower() in NULL_VALUES:
        return None
    if is_time and re.search(r"(?<!\d)1999(?!\d)", v):
        return None
    return v


def describe_result(raw):
    """Last Result value -> (code_or_None, readable text, ok True/False/None)."""
    if raw is None:
        return None, "unknown", None
    s = str(raw).strip()
    if not s or s.lower() in NULL_VALUES:
        return None, "unknown", None
    try:
        code = int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None, s, None
    if code < 0:
        code &= 0xFFFFFFFF
    if code in RESULT_CODES:
        text, ok = RESULT_CODES[code]
        return code, text, ok
    return code, f"Error (code {code} / 0x{code:X})", False


def _resolve_columns(header):
    norm = [h.strip().lower() for h in header]
    cols = {}
    for key, aliases in COLUMN_ALIASES.items():
        idx = None
        for alias in aliases:
            a = alias.lower()
            if a in norm:
                idx = norm.index(a)
                break
        if idx is None:
            idx = POSITIONAL_FALLBACK.get(key)
            if idx is not None and idx >= len(header):
                idx = None
        cols[key] = idx
    return cols


def _bat_from_action(action):
    if not action:
        return None
    m = re.findall(r"([^\\/\"'\s]+\.(?:bat|cmd|pyw?))\b", action, flags=re.IGNORECASE)
    return m[-1] if m else None


def parse_schtasks_csv(text, markers=TASK_NAME_MARKERS):
    """Pure: `schtasks /query /fo CSV /v` text -> list of task dicts whose
    short name contains one of `markers` (case-insensitive). Skips repeated
    header rows and dedupes multi-trigger rows (first row wins)."""
    rows = [r for r in csv.reader(io.StringIO(text or "")) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = rows[0]
    cols = _resolve_columns(header)
    name_idx = cols.get("task_name")
    if name_idx is None:
        return []
    lowered = [m.lower() for m in markers]
    out, seen = [], set()
    for row in rows[1:]:
        if row == header or (row and header and row[0].strip() == header[0].strip()):
            continue
        if len(row) <= name_idx:
            continue

        def get(key):
            i = cols.get(key)
            return row[i] if i is not None and i < len(row) else None

        path = (get("task_name") or "").strip()
        if not path or path in seen:
            continue
        short = path.replace("/", "\\").rstrip("\\").split("\\")[-1]
        if not any(m in short.lower() for m in lowered):
            continue
        seen.add(path)
        code, text_, ok = describe_result(get("last_result"))
        action = _clean(get("action"))
        out.append({
            "name": short,
            "path": path,
            "status": _clean(get("status")),
            "state": _clean(get("state")),
            "last_run": _clean(get("last_run"), is_time=True),
            "last_result_code": code,
            "last_result_text": text_,
            "ok": ok,
            "next_run": _clean(get("next_run"), is_time=True),
            "action": action,
            "bat": _bat_from_action(action),
        })
    return out


def _decode(data):
    if isinstance(data, str):
        return data
    data = data or b""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates = []
    if sys.platform == "win32":
        try:
            import ctypes
            cp = ctypes.windll.kernel32.GetConsoleOutputCP()
            if cp:
                candidates.append(f"cp{cp}")
        except Exception:
            pass
    try:
        candidates.append(locale.getpreferredencoding(False))
    except Exception:
        pass
    for enc in candidates:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    for enc in ("mbcs", "cp1254", "latin-1"):
        try:
            return data.decode(enc, errors="replace")
        except LookupError:
            continue
    return data.decode("latin-1", errors="replace")


def query_scheduled_tasks(timeout=SCHTASKS_TIMEOUT):
    """Runs the read-only scheduler query. Returns (rows, error_or_None);
    never raises."""
    kwargs = {"capture_output": True, "timeout": timeout}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(SCHTASKS_ARGS, **kwargs)
    except subprocess.TimeoutExpired:
        return [], f"schtasks timed out after {timeout}s"
    except FileNotFoundError:
        return [], "schtasks not found (not Windows?)"
    except Exception as e:
        return [], f"schtasks failed: {type(e).__name__}: {e}"
    try:
        if result.returncode != 0:
            err = _decode(result.stderr).strip() or _decode(result.stdout).strip()
            return [], f"schtasks exited with code {result.returncode}: {err[:300]}"
        return parse_schtasks_csv(_decode(result.stdout)), None
    except Exception as e:
        return [], f"could not parse schtasks output: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# (b) Daily run status
# --------------------------------------------------------------------------

RUN_STATUS_FIELDS = ("date", "state", "started_at", "finished_at", "steps", "summary",
                     "attention", "created_today", "plan_total", "content_summary",
                     "upload_totals")


def _step_failed(step):
    if not isinstance(step, dict):
        return False
    if step.get("ok") is False:
        return True
    status = str(step.get("status") or "").lower()
    return any(w in status for w in ("fail", "timeout", "timed out", "error", "crash"))


def load_run_status(path=None, today=None):
    """daily_run_status.json -> dict with state/is_today/stale/failed_steps.
    Missing/corrupt -> state 'unknown' with an 'error'. Never raises."""
    path = Path(path) if path else RUN_STATUS_FILE
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    try:
        if not path.exists():
            return {"state": "unknown", "error": f"{path.name} not found",
                    "is_today": False, "stale": False, "failed_steps": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        return {"state": "unknown", "error": f"could not read {path.name}: {type(e).__name__}",
                "is_today": False, "stale": False, "failed_steps": []}
    out = {k: data.get(k) for k in RUN_STATUS_FIELDS}
    if out["state"] not in KNOWN_STATES:
        out["state"] = "unknown"
    steps = out["steps"] if isinstance(out["steps"], list) else []
    out["steps"] = steps
    out["is_today"] = out["date"] == today
    out["stale"] = not out["is_today"]
    out["failed_steps"] = [
        str(s.get("name") or s.get("script") or "?") for s in steps if _step_failed(s)
    ]
    return out


# --------------------------------------------------------------------------
# (c) Other last-run files
# --------------------------------------------------------------------------

def _read_json(path):
    """-> (data, error). Missing -> (None, None)."""
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"could not read {path.name}: {type(e).__name__}"


def load_last_run_rows(repo_dir=None):
    """-> (rows, errors); rows are {source, job, when, detail}."""
    base = Path(repo_dir) if repo_dir else REPO_DIR
    rows, errors = [], []

    hm, err = _read_json(base / HM_LAST_RUN_NAME)
    if err:
        errors.append(err)
    elif isinstance(hm, dict):
        when = next((hm.get(k) for k in ("date", "when", "finished_at", "run_at", "started_at")
                     if hm.get(k)), None)
        decisions = hm.get("decisions")
        n_dec = len(decisions) if isinstance(decisions, (dict, list)) else 0
        rf = hm.get("read_failed")
        n_rf = len(rf) if isinstance(rf, (dict, list)) else (rf if isinstance(rf, int) else 0)
        rows.append({"source": HM_LAST_RUN_NAME, "job": "hm_stock", "when": when,
                     "detail": f"decisions={n_dec}, read_failed={n_rf}"})
    elif hm is not None:
        errors.append(f"could not read {HM_LAST_RUN_NAME}: not a JSON object")

    cat, err = _read_json(base / CATALOG_STATUS_NAME)
    if err:
        errors.append(err)
    elif isinstance(cat, dict):
        runs = cat.get("runs")
        if isinstance(runs, dict):
            for job, info in runs.items():
                info = info if isinstance(info, dict) else {}
                rows.append({"source": CATALOG_STATUS_NAME, "job": str(job),
                             "when": info.get("when"),
                             "detail": f"ok={info.get('ok')}, fresh={info.get('fresh')}"})
    elif cat is not None:
        errors.append(f"could not read {CATALOG_STATUS_NAME}: not a JSON object")
    return rows, errors


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def collect_status(now=None, *, repo_dir=None, run_schtasks=True):
    """Everything the 'Daily tasks' view needs. Never raises."""
    result = {"collected_at": None, "today": None, "tasks": [], "daily_run": {"state": "unknown"},
              "last_runs": [], "errors": []}
    try:
        now = now or datetime.now()
        result["collected_at"] = now.isoformat(timespec="seconds")
        result["today"] = now.strftime("%Y-%m-%d")
        base = Path(repo_dir) if repo_dir else REPO_DIR

        if run_schtasks:
            tasks, err = query_scheduled_tasks()
            result["tasks"] = tasks
            if err:
                result["tasks_error"] = err
                result["errors"].append(err)

        result["daily_run"] = load_run_status(base / RUN_STATUS_NAME, today=result["today"])
        if result["daily_run"].get("error"):
            result["errors"].append(result["daily_run"]["error"])

        rows, errs = load_last_run_rows(base)
        result["last_runs"] = rows
        result["errors"].extend(errs)
    except Exception as e:  # pragma: no cover - defensive
        result["errors"].append(f"collect_status failed: {type(e).__name__}: {e}")
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _short(value, limit=120):
    v = "" if value is None else str(value)
    return v if len(v) <= limit else v[:limit - 3] + "..."


def _table(headers, rows):
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(lines)


def main():
    st = collect_status()
    print(f"Collected {st['collected_at']}\n")
    print("Scheduled tasks")
    if st["tasks"]:
        print(_table(["Name", "Status", "Last run", "Result", "Next run", "Bat"],
                     [[t["name"], t["status"], t["last_run"], t["last_result_text"],
                       t["next_run"], t["bat"]] for t in st["tasks"]]))
    else:
        print("  (none found)" + (f" -- {st['tasks_error']}" if st.get("tasks_error") else ""))

    dr = st["daily_run"]
    print("\nDaily run")
    print(_table(["Field", "Value"], [
        ["date", dr.get("date")],
        ["state", dr.get("state")],
        ["today", "yes" if dr.get("is_today") else ("STALE" if dr.get("stale") else "n/a")],
        ["started", dr.get("started_at")],
        ["finished", dr.get("finished_at")],
        ["failed steps", ", ".join(dr.get("failed_steps") or []) or "-"],
        ["summary", _short(dr.get("summary"))],
    ]))

    print("\nOther last runs")
    if st["last_runs"]:
        print(_table(["Source", "Job", "When", "Detail"],
                     [[r["source"], r["job"], r["when"], r["detail"]] for r in st["last_runs"]]))
    else:
        print("  (none)")

    if st["errors"]:
        print("\nErrors")
        for e in st["errors"]:
            print(f"  - {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
