"""
Shared margin-ratio logic used by campaign enrollment and margin-risk
scanning (business instruction, 2026-09-02: "if a product is below 46%
product cost when enrolled with max boost price than enroll it with max
boost price" + later: catch live-price drift and re-check the whole
catalog). Centralized here so enroll_campaigns.py and the margin-scan step
don't each reimplement the same USD/TRY conversion, M&S price lookup, and
real-stock check.

Ratio definition throughout: M&S cost (USD) / actual selling price (USD),
as a percentage. A product QUALIFIES (healthy margin) when ratio <= 46%
(inclusive -- confirmed with the business, 2026-09-02, after finding two
real candidates sitting at exactly 46.0%). Anything > 46% is thin/risky
margin.

"Real stock" means RFBS (seller-fulfilled) inventory via
/v4/product/info/stocks -- NOT the stock field on /v1/actions/candidates or
/v1/actions/products, which reads 0 for this account's entire real
inventory (confirmed live, 2026-09-02: candidates showing stock=0 turned
out to have 28, 22, 2 units actually in stock). Every eligibility/removal
decision in this pipeline must go through get_real_stock, never trust a
campaign endpoint's own stock field.
"""
import json

import requests

from ozon_client import call

MARGIN_RATIO_THRESHOLD = 46.0  # qualifies when ratio <= this value
PRICE_HISTORY_FILE = "price_history.jsonl"


def fetch_usd_try_rate():
    """Live USD->TRY rate, cross-checked against two independent sources
    (confirmed live, 2026-09-02: both agreed to 2 decimal places). Raises
    if neither source is reachable -- callers should not silently fall back
    to a stale/guessed rate for a live financial calculation."""
    sources = [
        "https://api.exchangerate-api.com/v4/latest/USD",
        "https://open.er-api.com/v6/latest/USD",
    ]
    rates = []
    for url in sources:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            rates.append(float(resp.json()["rates"]["TRY"]))
        except Exception:
            continue
    if not rates:
        raise RuntimeError("Could not fetch USD/TRY rate from any source.")
    return rates[0]


def load_ms_prices(path=PRICE_HISTORY_FILE):
    """offer_id -> (price_try, url), from the most recent entry per
    offer_id in today's (or the latest available) price_history.jsonl."""
    prices = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                prices[entry["offer_id"]] = (float(entry["price"]), entry.get("url"))
    except FileNotFoundError:
        pass
    return prices


def resolve_offer_ids_and_names(product_ids):
    """product_id -> (offer_id, name) via /v3/product/info/list.

    NOTE: this endpoint's response is FLAT ({"items": [...]}), not nested
    under "result" -- confirmed live 2026-09-02 that the previously-used
    result.get("result", {}).get("items", []) silently returned nothing,
    causing every name/offer_id resolution in campaign_eligibility_today.json
    to come back null."""
    mapping = {}
    product_ids = list(product_ids)
    for i in range(0, len(product_ids), 1000):
        batch = product_ids[i:i + 1000]
        if not batch:
            continue
        info = call("/v3/product/info/list", {"product_id": batch})
        for item in info.get("items", []):
            mapping[item["id"]] = (item.get("offer_id"), item.get("name"))
    return mapping


def get_real_stock(offer_ids):
    """offer_id -> total real (RFBS + any other present) stock via
    /v4/product/info/stocks. This is the ONLY reliable stock source for
    this account -- see module docstring."""
    stock = {}
    offer_ids = [oid for oid in offer_ids if oid]
    for i in range(0, len(offer_ids), 100):
        batch = offer_ids[i:i + 100]
        if not batch:
            continue
        result = call("/v4/product/info/stocks", {"filter": {"offer_id": batch, "visibility": "ALL"}, "limit": 100})
        for item in result.get("items", []):
            stock[item["offer_id"]] = sum(s.get("present", 0) for s in item.get("stocks", []))
    return stock


def compute_ratio_pct(ms_price_try, live_price_usd, usd_try_rate):
    """M&S cost (converted to USD) as a percentage of the live selling
    price. Returns None if live_price_usd is falsy (can't divide)."""
    if not live_price_usd:
        return None
    ms_price_usd = ms_price_try / usd_try_rate
    return (ms_price_usd / live_price_usd) * 100


def qualifies(ratio_pct):
    """True when ratio_pct is a healthy margin (<= 46%, inclusive)."""
    return ratio_pct is not None and ratio_pct <= MARGIN_RATIO_THRESHOLD
