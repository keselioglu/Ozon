"""daily_report posting lists: new cursor endpoints + new price shape (2026-11-25)."""
from datetime import datetime, timezone

import daily_report as d


def test_cursor_paging_and_new_price_shape(monkeypatch):
    calls = []
    pages = [
        {"postings": [{"status": "awaiting_deliver", "products": [
            {"offer_id": "MS-A-1", "price": {"amount": "158.04", "currency": "USD"}, "quantity": 1}]}],
         "has_next": True, "cursor": "c1"},
        {"postings": [{"status": "delivered", "products": [
            {"offer_id": "MS-B-2", "price": "10.00", "currency_code": "USD", "quantity": 2}]}],
         "has_next": False, "cursor": ""},
    ]

    def fake_call(path, payload):
        calls.append((path, dict(payload)))
        return pages[len(calls) - 1]

    monkeypatch.setattr(d, "call", fake_call)
    t0 = datetime(2026, 11, 24, tzinfo=timezone.utc)
    out = d.fetch_fbs_postings(t0, datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert [c[0] for c in calls] == ["/v4/posting/fbs/list"] * 2
    assert "cursor" not in calls[0][1] and calls[1][1]["cursor"] == "c1"
    assert "offset" not in calls[0][1]
    a, b = out[0]["products"][0], out[1]["products"][0]
    assert (a["price"], a["currency_code"]) == ("158.04", "USD")
    assert (b["price"], b["currency_code"]) == ("10.00", "USD")


def test_fbo_uses_v3(monkeypatch):
    seen = []
    monkeypatch.setattr(d, "call", lambda path, payload: seen.append(path) or {"result": {"postings": [], "has_next": False}})
    t = datetime(2026, 11, 24, tzinfo=timezone.utc)
    assert d.fetch_fbo_postings(t, t) == []
    assert seen == ["/v3/posting/fbo/list"]
