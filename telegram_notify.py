"""
Safe Telegram sender for short pipeline notifications (task #49).

`send_telegram(text)` posts a plain-text message to the chat configured by
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (read from .env via python-dotenv, and
from os.environ at call time). It never raises and returns True only when
Telegram answered HTTP 200 with `"ok": true`.

Safety rules:
- Not configured (either variable missing/empty) -> prints
  "Telegram not sent: not configured", no network call, returns False.
- No parse_mode: plain text, so product names or '_' can't break parsing.
- Text is truncated to MAX_TEXT_LEN (4000; Telegram's limit is 4096).
- Retries once on a network error or HTTP 5xx.
- The bot token, the full request URL and raw requests exception strings
  (which contain the URL, and so the token) are never printed. Failure
  reasons are the HTTP status plus Telegram's `description`, or the
  exception class name, and the token is redacted from them regardless.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.telegram.org"
MAX_TEXT_LEN = 4000
RETRY_DELAY_SECONDS = 2


def _redact(value, token):
    s = str(value)
    if token:
        s = s.replace(token, "<redacted>")
    return s


def _fail(reason, token):
    print(f"Telegram not sent: {_redact(reason, token)}")
    return False


def send_telegram(text, *, timeout=20):
    """Send `text` as a plain Telegram message. Returns True on success, never raises."""
    token = ""
    try:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            print("Telegram not sent: not configured")
            return False

        body = "" if text is None else str(text)
        if len(body) > MAX_TEXT_LEN:
            body = body[:MAX_TEXT_LEN]
        payload = {"chat_id": chat_id, "text": body, "disable_web_page_preview": True}
        url = f"{API_BASE}/bot{token}/sendMessage"

        reason = "unknown error"
        for attempt in range(2):
            if attempt:
                time.sleep(RETRY_DELAY_SECONDS)
            try:
                resp = requests.post(url, json=payload, timeout=timeout)
            except requests.exceptions.RequestException as e:
                reason = type(e).__name__
                continue

            status = resp.status_code
            try:
                data = resp.json()
            except Exception:
                data = None
            description = data.get("description") if isinstance(data, dict) else None

            if status == 200 and isinstance(data, dict) and data.get("ok") is True:
                print("Telegram message sent.")
                return True

            reason = f"HTTP {status}"
            if description:
                reason += f" {description}"
            elif status == 200:
                reason += " response not ok"
            if status >= 500:
                continue
            break
        return _fail(reason, token)
    except Exception as e:  # never raise
        try:
            return _fail(type(e).__name__, token)
        except Exception:
            return False
