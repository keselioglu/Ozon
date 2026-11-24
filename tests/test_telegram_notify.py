"""Offline tests for telegram_notify.py -- requests.post is always monkeypatched."""
import requests
import pytest

import telegram_notify as tn

FAKE_TOKEN = "123456:FAKE-TOKEN-abcDEF_xyz"
FAKE_CHAT = "987654"


class FakeResp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def _no_network(*a, **k):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(tn.requests, "post", _no_network)
    monkeypatch.setattr(tn.time, "sleep", lambda s: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def _configure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT)


def _recorder(monkeypatch, responses):
    calls = []
    seq = list(responses)

    def fake_post(url, json=None, timeout=None, **kw):
        calls.append({"url": url, "json": json, "timeout": timeout})
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(tn.requests, "post", fake_post)
    return calls


def _out(capsys):
    out = capsys.readouterr()
    text = out.out + out.err
    assert FAKE_TOKEN not in text
    return text


def test_not_configured_both_missing(capsys):
    assert tn.send_telegram("x") is False
    assert _out(capsys).strip() == "Telegram not sent: not configured"


@pytest.mark.parametrize("present", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_not_configured_one_missing(monkeypatch, capsys, present):
    monkeypatch.setenv(present, FAKE_TOKEN if present == "TELEGRAM_BOT_TOKEN" else FAKE_CHAT)
    assert tn.send_telegram("x") is False
    assert _out(capsys).strip() == "Telegram not sent: not configured"


def test_not_configured_empty_values(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert tn.send_telegram("x") is False
    assert _out(capsys).strip() == "Telegram not sent: not configured"


def test_success(monkeypatch, capsys):
    _configure(monkeypatch)
    calls = _recorder(monkeypatch, [FakeResp(200, {"ok": True, "result": {}})])
    assert tn.send_telegram("hello_world *x*", timeout=7) is True
    assert len(calls) == 1
    assert calls[0]["url"] == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert calls[0]["timeout"] == 7
    assert calls[0]["json"] == {
        "chat_id": FAKE_CHAT, "text": "hello_world *x*", "disable_web_page_preview": True,
    }
    assert _out(capsys).strip() == "Telegram message sent."


def test_no_parse_mode_in_payload(monkeypatch, capsys):
    _configure(monkeypatch)
    calls = _recorder(monkeypatch, [FakeResp(200, {"ok": True})])
    tn.send_telegram("a_b")
    assert "parse_mode" not in calls[0]["json"]
    _out(capsys)


def test_http_400_with_description(monkeypatch, capsys):
    _configure(monkeypatch)
    calls = _recorder(monkeypatch, [FakeResp(400, {"ok": False, "description": "Bad Request: chat not found"})])
    assert tn.send_telegram("x") is False
    assert len(calls) == 1  # 4xx is not retried
    assert _out(capsys).strip() == "Telegram not sent: HTTP 400 Bad Request: chat not found"


def test_200_but_not_ok(monkeypatch, capsys):
    _configure(monkeypatch)
    _recorder(monkeypatch, [FakeResp(200, {"ok": False})])
    assert tn.send_telegram("x") is False
    assert _out(capsys).startswith("Telegram not sent: HTTP 200")


def test_exception_containing_token_is_not_printed(monkeypatch, capsys):
    _configure(monkeypatch)
    url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    exc = requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}")
    calls = _recorder(monkeypatch, [exc])
    assert tn.send_telegram("x") is False
    assert len(calls) == 2  # retried once
    text = _out(capsys)
    assert text.strip() == "Telegram not sent: ConnectionError"
    assert "api.telegram.org" not in text


def test_retry_once_on_5xx_then_success(monkeypatch, capsys):
    _configure(monkeypatch)
    calls = _recorder(monkeypatch, [FakeResp(502, {"ok": False}), FakeResp(200, {"ok": True})])
    assert tn.send_telegram("x") is True
    assert len(calls) == 2
    _out(capsys)


def test_5xx_twice_fails(monkeypatch, capsys):
    _configure(monkeypatch)
    calls = _recorder(monkeypatch, [FakeResp(500, ValueError("not json"))])
    assert tn.send_telegram("x") is False
    assert len(calls) == 2
    assert _out(capsys).strip() == "Telegram not sent: HTTP 500"


def test_description_with_token_is_redacted(monkeypatch, capsys):
    _configure(monkeypatch)
    _recorder(monkeypatch, [FakeResp(401, {"ok": False, "description": f"Unauthorized {FAKE_TOKEN}"})])
    assert tn.send_telegram("x") is False
    assert "<redacted>" in _out(capsys)


def test_truncation(monkeypatch, capsys):
    _configure(monkeypatch)
    calls = _recorder(monkeypatch, [FakeResp(200, {"ok": True})])
    assert tn.send_telegram("y" * 5000) is True
    assert len(calls[0]["json"]["text"]) == 4000
    _out(capsys)


def test_unexpected_exception_never_raises(monkeypatch, capsys):
    _configure(monkeypatch)
    _recorder(monkeypatch, [RuntimeError(f"boom {FAKE_TOKEN}")])
    assert tn.send_telegram("x") is False
    assert _out(capsys).strip() == "Telegram not sent: RuntimeError"
