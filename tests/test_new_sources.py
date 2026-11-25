import ast
import json
import os
from datetime import datetime, timezone

import pytest

import new_sources

NOW = datetime(2026, 11, 25, 9, 30, tzinfo=timezone.utc)

BOARD = (
    "# Project Task Board\r\n"
    "\r\n"
    "Intro text.\r\n"
    "\r\n"
    "---\r\n"
    "\r\n"
    "## Todo\r\n"
    "\r\n"
    "- Existing task\r\n"
    "  - sub item\r\n"
    "\r\n"
    "---\r\n"
    "\r\n"
    "## Done\r\n"
    "- old thing\r\n"
)


def _paths(tmp_path, board=BOARD):
    req = tmp_path / "new_source_requests.json"
    tasks = tmp_path / "TASKS.md"
    if board is not None:
        tasks.write_bytes(board.encode("utf-8"))
    return str(req), str(tasks)


def _read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def test_valid_request_writes_both_files(tmp_path):
    req, tasks = _paths(tmp_path)
    rec = new_sources.create_request("https://www.Example.com/shop/", "  Kids clothes  ",
                                     requests_path=req, tasks_path=tasks, now=NOW)
    assert rec["id"] == 1
    assert rec["url"] == "https://www.Example.com/shop/"
    assert rec["description"] == "Kids clothes"
    assert rec["status"] == "requested"
    assert rec["created_at"].startswith("2026-11-25")

    with open(req, encoding="utf-8") as f:
        assert json.load(f) == [rec]

    text = _read(tasks)
    head = "## Todo\r\n\r\n"
    idx = text.index(head) + len(head)
    epic = ("- EPIC: Add new product source www.example.com (https://www.Example.com/shop/)"
            " — Kids clothes (requested 2026-11-25)\r\n")
    assert text[idx:idx + len(epic)] == epic
    block = new_sources.epic_block(rec, "\r\n")
    assert block.startswith(epic)
    for sub in ("Website check (structure, categories, product pages)", "Crawler",
                "Category/type mapping", "Translation", "Upload/brand prefix",
                "Stock/price sync"):
        assert "\r\n  - %s\r\n" % sub in block
    # Rest of the file unchanged byte-for-byte.
    assert text[:idx] + text[idx + len(block):] == BOARD
    assert text[idx:idx + len(block)] == block


def test_second_request_gets_next_id_and_goes_on_top(tmp_path):
    req, tasks = _paths(tmp_path)
    new_sources.create_request("https://a.com", "A", requests_path=req, tasks_path=tasks, now=NOW)
    rec = new_sources.create_request("https://b.com", "B", requests_path=req, tasks_path=tasks, now=NOW)
    assert rec["id"] == 2
    text = _read(tasks)
    assert text.index("b.com") < text.index("a.com")
    with open(req, encoding="utf-8") as f:
        assert [r["id"] for r in json.load(f)] == [1, 2]


@pytest.mark.parametrize("url", ["ftp://example.com", "abc", "", "   ", "https://", "http:///path"])
def test_invalid_url_refused_nothing_written(tmp_path, url):
    req, tasks = _paths(tmp_path)
    assert new_sources.validate_request(url, "desc")
    with pytest.raises(ValueError):
        new_sources.create_request(url, "desc", requests_path=req, tasks_path=tasks, now=NOW)
    assert not os.path.exists(req)
    assert _read(tasks) == BOARD


@pytest.mark.parametrize("desc", ["", "   \n\t", "x" * 1001])
def test_bad_description_refused_nothing_written(tmp_path, desc):
    req, tasks = _paths(tmp_path)
    assert new_sources.validate_request("https://example.com", desc)
    with pytest.raises(ValueError):
        new_sources.create_request("https://example.com", desc, requests_path=req,
                                   tasks_path=tasks, now=NOW)
    assert not os.path.exists(req)
    assert _read(tasks) == BOARD


def test_description_limit_inclusive():
    assert new_sources.validate_request("https://example.com", "x" * 1000) == []


def test_duplicate_url_refused(tmp_path):
    req, tasks = _paths(tmp_path)
    new_sources.create_request("https://www.example.com/shop", "first", requests_path=req,
                               tasks_path=tasks, now=NOW)
    before_tasks = _read(tasks)
    before_req = _read(req)
    with pytest.raises(ValueError, match="already requested"):
        new_sources.create_request("https://WWW.EXAMPLE.COM/shop/", "again", requests_path=req,
                                   tasks_path=tasks, now=NOW)
    assert _read(tasks) == before_tasks
    assert _read(req) == before_req


def test_corrupt_requests_file_backed_up_and_recovered(tmp_path):
    req, tasks = _paths(tmp_path)
    with open(req, "w", encoding="utf-8") as f:
        f.write("{not json")
    rec = new_sources.create_request("https://example.com", "desc", requests_path=req,
                                     tasks_path=tasks, now=NOW)
    with open(req + ".bak", encoding="utf-8") as f:
        assert f.read() == "{not json"
    with open(req, encoding="utf-8") as f:
        assert json.load(f) == [rec]


def test_requests_file_not_a_list_is_corrupt(tmp_path):
    req, tasks = _paths(tmp_path)
    with open(req, "w", encoding="utf-8") as f:
        json.dump({"a": 1}, f)
    new_sources.create_request("https://example.com", "desc", requests_path=req,
                               tasks_path=tasks, now=NOW)
    assert os.path.exists(req + ".bak")


def test_tasks_without_todo_heading_gets_one(tmp_path):
    board = "# Board\n\n## Done\n- x\n"
    req, tasks = _paths(tmp_path, board)
    rec = new_sources.create_request("https://example.com", "desc", requests_path=req,
                                     tasks_path=tasks, now=NOW)
    text = _read(tasks)
    assert text.startswith(board)
    assert text[len(board):] == "\n## Todo\n\n" + new_sources.epic_block(rec)


def test_missing_tasks_file_is_created(tmp_path):
    req, tasks = _paths(tmp_path, None)
    rec = new_sources.create_request("https://example.com", "desc", requests_path=req,
                                     tasks_path=tasks, now=NOW)
    assert _read(tasks) == "## Todo\n\n" + new_sources.epic_block(rec)


def test_normalize_url():
    n = new_sources.normalize_url
    assert n("https://Example.COM/") == n("https://example.com")
    assert n("https://example.com/a/") == n("https://example.com/a")
    assert n("https://example.com/a") != n("https://example.com/b")
    assert n("abc") is None


def test_module_does_not_import_network_libraries():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "new_sources.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update("%s.%s" % (node.module, a.name) for a in node.names)
    banned = {"requests", "urllib.request", "http.client", "socket", "urllib3", "httpx"}
    assert not (imported & banned), imported & banned
    assert not any(m.startswith(("requests.", "urllib.request.")) for m in imported)
