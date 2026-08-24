import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api-seller.ozon.ru"
CLIENT_ID = os.environ.get("OZON_CLIENT_ID")
API_KEY = os.environ.get("OZON_API_KEY")


def _headers():
    return {
        "Client-Id": CLIENT_ID,
        "Api-Key": API_KEY,
        "Content-Type": "application/json",
    }


def call(method_path, payload=None, retries=3):
    """POST to an Ozon Seller API method. Retries on 429/5xx with backoff."""
    if not CLIENT_ID or not API_KEY:
        raise RuntimeError("OZON_CLIENT_ID / OZON_API_KEY missing. Put them in .env (see .env.example).")

    url = f"{BASE_URL}{method_path}"
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload or {}, headers=_headers(), timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Ozon API error {resp.status_code} on {method_path}: {resp.text[:500]}")
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed after {retries} attempts calling {method_path}: {last_error}")


def check_connection():
    """Cheap read-only call to confirm credentials work."""
    return call("/v2/warehouse/list")
