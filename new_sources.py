"""
"Add a new website" requests: records a request to add a new product source
(another shop's website) and opens an epic for it on the TASKS.md board.

Tk-free and offline: the URL is only parsed, never fetched (no network
imports on purpose -- tests/test_new_sources.py checks this). The only
writes are two local files:

- new_source_requests.json (gitignored): a JSON list of
  {"id", "url", "description", "created_at", "status": "requested"},
  written atomically. A corrupt file is kept as <file>.bak and restarted.
- TASKS.md: an EPIC bullet block inserted directly under the "## Todo"
  heading (a Todo section is appended if the heading is missing). The rest
  of the file is left byte-for-byte unchanged, line endings included.
"""
import json
import os
import re
import tempfile
from datetime import datetime
from urllib.parse import urlsplit

REQUESTS_FILE = "new_source_requests.json"
TASKS_FILE = "TASKS.md"
MAX_DESCRIPTION = 1000
STATUS_REQUESTED = "requested"

EPIC_SUBTASKS = (
    "Website check (structure, categories, product pages)",
    "Crawler",
    "Category/type mapping",
    "Translation",
    "Upload/brand prefix",
    "Stock/price sync",
)

_TODO_RE = re.compile(r"^##[ \t]+Todo[ \t]*$", re.IGNORECASE)


# --- Validation -------------------------------------------------------------------

def _parse(url):
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname  # may raise ValueError on a bad port/netloc
        _ = parts.port
    except (ValueError, AttributeError):
        return None, None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None, None
    return parts, host.lower()


def validate_request(url, description):
    """List of human-readable errors; empty means the request is valid."""
    errors = []
    if not isinstance(url, str) or not url.strip():
        errors.append("URL is required.")
    elif _parse(url)[0] is None:
        errors.append("URL must start with http:// or https:// and include a host "
                      "(e.g. https://www.example.com).")
    if not isinstance(description, str) or not description.strip():
        errors.append("Description is required.")
    elif len(description.strip()) > MAX_DESCRIPTION:
        errors.append("Description must be at most %d characters (it has %d)."
                      % (MAX_DESCRIPTION, len(description.strip())))
    return errors


def normalize_url(url):
    """Key for duplicate checks: lower-case scheme and host, no trailing
    slash on the path. None if the URL doesn't parse."""
    parts, host = _parse(url) if isinstance(url, str) else (None, None)
    if parts is None:
        return None
    netloc = host
    if parts.port:
        netloc = "%s:%d" % (host, parts.port)
    path = parts.path.rstrip("/")
    out = "%s://%s%s" % (parts.scheme.lower(), netloc, path)
    if parts.query:
        out += "?" + parts.query
    return out


# --- Requests file ----------------------------------------------------------------

def load_requests(path=REQUESTS_FILE):
    """The saved list of requests. Missing file -> []. A corrupt file (bad
    JSON or not a list) is renamed to <path>.bak and [] is returned."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        data = None
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    return []


def _write_atomic_text(text, path, prefix):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _next_id(requests_list):
    ids = [r.get("id") for r in requests_list if isinstance(r.get("id"), int)]
    return (max(ids) + 1) if ids else 1


# --- TASKS.md ---------------------------------------------------------------------

def epic_block(record, newline="\n"):
    host = urlsplit(record["url"]).hostname.lower()
    desc = " ".join(record["description"].split())
    date = str(record["created_at"])[:10]
    lines = ["- EPIC: Add new product source %s (%s) — %s (requested %s)"
             % (host, record["url"], desc, date)]
    lines += ["  - %s" % s for s in EPIC_SUBTASKS]
    return "".join(line + newline for line in lines)


def insert_epic(text, block_lf):
    """Returns text with the epic block inserted under '## Todo' (after the
    heading's blank line, if there is one). Appends a Todo section if no
    heading is found. block_lf uses '\\n'; it's converted to the file's
    line ending."""
    newline = "\r\n" if "\r\n" in text else "\n"
    block = block_lf.replace("\n", newline)
    lines = text.splitlines(keepends=True)
    offset = 0
    for i, line in enumerate(lines):
        if _TODO_RE.match(line.rstrip("\r\n")):
            pos = offset + len(line)
            if not line.endswith("\n"):  # heading is the last line, no newline
                return text + newline + newline + block
            if i + 1 < len(lines) and not lines[i + 1].strip():
                pos += len(lines[i + 1])
            return text[:pos] + block + text[pos:]
        offset += len(line)
    prefix = text
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix:
        prefix += newline
    return prefix + "## Todo" + newline + newline + block


# --- Public API -------------------------------------------------------------------

def create_request(url, description, requests_path=REQUESTS_FILE,
                   tasks_path=TASKS_FILE, now=None):
    """Validates, refuses duplicates, writes the epic into TASKS.md and the
    record into the requests file. Returns the record. Raises ValueError
    (with all error messages) if the request is refused; nothing is written
    in that case."""
    errors = validate_request(url, description)
    if errors:
        raise ValueError(" ".join(errors))
    url = url.strip()
    description = description.strip()
    existing = load_requests(requests_path)
    key = normalize_url(url)
    for r in existing:
        if normalize_url(r.get("url") or "") == key:
            raise ValueError("This website was already requested (request #%s, %s)."
                             % (r.get("id"), str(r.get("created_at", ""))[:10]))
    now = now or datetime.now().astimezone()
    record = {
        "id": _next_id(existing),
        "url": url,
        "description": description,
        "created_at": now.isoformat(timespec="seconds"),
        "status": STATUS_REQUESTED,
    }

    try:
        with open(tasks_path, encoding="utf-8", newline="") as f:
            tasks_text = f.read()
    except FileNotFoundError:
        tasks_text = ""
    new_tasks = insert_epic(tasks_text, epic_block(record))
    _write_atomic_text(new_tasks, tasks_path, ".tasks_")

    _write_atomic_text(json.dumps(existing + [record], ensure_ascii=False, indent=2) + "\n",
                       requests_path, ".new_source_requests_")
    return record
