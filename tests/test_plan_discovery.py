"""Offline tests: category discovery and quota-fill aim for the daily plan."""
import json

import pytest

import category_priority as cp
import crawler
import daily_plan


def _rows(name, n_in_stock, n_out=0, code=None):
    rows = [{"name": name, "ms_article_code": code, "parent_sku": None,
             "stock_count": 3, "in_stock": True} for _ in range(n_in_stock)]
    rows += [{"name": name, "ms_article_code": code, "parent_sku": None,
              "stock_count": 0, "in_stock": False} for _ in range(n_out)]
    return rows


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Two categories: 'Çorap' (socks, 10 products x 2 offers) then
    'Pijama' (pajamas, 10 products x 2 offers)."""
    monkeypatch.chdir(tmp_path)
    categories = [
        {"Category Name (TR)": "Çorap", "Full URL": "https://x/corap/", "Proprity": 1},
        {"Category Name (TR)": "Pijama", "Full URL": "https://x/pijama/", "Proprity": 2},
    ]
    pages = {}
    for cat, prefix, label_name in (("https://x/corap/", "sock", "Çorap"),
                                    ("https://x/pijama/", "pj", "Pijama Takımı")):
        urls = {f"https://x/p/{prefix}{i:02d}" for i in range(10)}
        pages[cat] = urls
    monkeypatch.setattr(cp, "load_priority_categories", lambda path=None: categories)
    monkeypatch.setattr(cp, "is_supported_category", lambda name, url=None: True)
    monkeypatch.setattr(cp, "fetch_live_ms_identifiers", lambda: set())
    monkeypatch.setattr(cp, "discover_category_products", lambda url: set(pages[url]))

    fetched = []

    def fake_extract(url):
        fetched.append(url)
        name = "Çorap" if "/sock" in url else "Pijama Takımı"
        return _rows(name, 2, code=url.rsplit("/", 1)[-1].upper())

    monkeypatch.setattr(crawler, "extract_product", fake_extract)
    monkeypatch.setattr(daily_plan, "category_label_for_name",
                        lambda n: {"Çorap": "Socks", "Pijama Takımı": "Pajama"}.get(n))
    return fetched


def _queued():
    with open(cp.URLS_FILE, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def test_estimate_offers():
    assert cp.estimate_offers(_rows("a", 3, 2)) == 3
    unknown = [{"stock_count": None, "in_stock": None}] * 4
    assert cp.estimate_offers(unknown) == 4
    assert cp.estimate_offers([{"stock_count": None, "in_stock": False}]) == 0
    assert cp.estimate_offers([]) == 0


def test_cycle_stops_near_target_times_factor(site):
    summary = cp.run_priority_cycle(target_new_products=10,
                                    per_category_target={("M&S", "Socks"): 4},
                                    fill_remainder=True)
    assert cp.DISCOVERY_OVERSHOOT_FACTOR == 1.2
    assert summary["stopped_reason"] == "plan_target_reached"
    assert summary["offer_target"] == 12
    assert 12 <= summary["estimated_offers_queued"] <= 13
    assert len(_queued()) == summary["new_urls_found"] == 6
    # Stopped fetching pages once the target was reached.
    assert len(site) == 6


def test_full_category_stops_without_fill_remainder(site):
    summary = cp.run_priority_cycle(target_new_products=10,
                                    per_category_target={("M&S", "Socks"): 4},
                                    fill_remainder=False)
    queued = _queued()
    socks = [u for u in queued if "/sock" in u]
    # 4 x 1.2 = 4.8 -> 5 offers cap; 2 offers per product -> 3 products (6 offers).
    assert len(socks) == 3
    assert summary["queued_by_category"] == {"M&S/Socks": 6}
    # Pajama has no allocation and fill_remainder is off -> nothing queued.
    assert not [u for u in queued if "/pj" in u]
    assert summary["stopped_reason"] == "exhausted_all_categories"
    assert summary["skipped_category_full"] == 17


def test_fill_remainder_continues_into_other_categories(site):
    summary = cp.run_priority_cycle(target_new_products=20,
                                    per_category_target={("M&S", "Socks"): 4},
                                    fill_remainder=True)
    queued = _queued()
    assert len([u for u in queued if "/sock" in u]) == 10  # total still had room
    assert len([u for u in queued if "/pj" in u]) == 2
    assert summary["estimated_offers_queued"] == 24


def test_discovery_counts_recorded_and_merged(site):
    with open(cp.INVENTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"other_key": 1}, f)
    cp.run_priority_cycle(target_new_products=None, per_category_target=None)
    with open(cp.INVENTORY_FILE, encoding="utf-8") as f:
        data = json.load(f)
    assert data["other_key"] == 1
    counts = data["discovery_found"]["counts"]["M&S"]
    assert counts["Socks"] == {"products": 10, "offers": 20}
    assert counts["Pajama"] == {"products": 10, "offers": 20}


def test_is_supported_category_still_delegates(monkeypatch):
    seen = []
    monkeypatch.setattr(cp, "is_known_product_type", lambda n: seen.append(n) or True)
    assert cp.is_supported_category("Çorap", "https://x/") is True
    assert seen == ["Çorap"]


def test_fill_remaining_quota_exits_when_plan_met(tmp_path, monkeypatch):
    import fill_remaining_quota as frq
    import upload_to_ozon

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(frq, "LOG_FILE", tmp_path / "log.txt")
    monkeypatch.setattr(upload_to_ozon, "check_quota",
                        lambda: {"daily_create": {"limit": 2000, "usage": 500}})
    monkeypatch.setattr(daily_plan, "created_today", lambda *a, **k: (500, {}))
    calls = []
    monkeypatch.setattr(frq, "run_step", lambda *a, **k: calls.append(a) or (True, ""))
    monkeypatch.setattr(frq, "post_github_report", lambda body: calls.append(body))
    frq.main()
    assert calls == []
    assert "Daily plan (500) already met — nothing to fill" in (tmp_path / "log.txt").read_text(encoding="utf-8")


def test_fill_remaining_quota_runs_when_plan_open(tmp_path, monkeypatch):
    import fill_remaining_quota as frq
    import upload_to_ozon

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(frq, "LOG_FILE", tmp_path / "log.txt")
    monkeypatch.setattr(upload_to_ozon, "check_quota",
                        lambda: {"daily_create": {"limit": 2000, "usage": 100}})
    monkeypatch.setattr(daily_plan, "created_today", lambda *a, **k: (100, {}))
    calls = []
    monkeypatch.setattr(frq, "run_step", lambda name, args, timeout=0: calls.append(args) or (False, ""))
    monkeypatch.setattr(frq, "post_github_report", lambda body: None)
    frq.main()
    assert calls == [["category_priority.py"]]


def test_main_skips_walk_when_plan_met(tmp_path, monkeypatch, capsys):
    import upload_to_ozon

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(upload_to_ozon, "check_quota",
                        lambda: {"daily_create": {"limit": 2000, "usage": 600}})
    monkeypatch.setattr(daily_plan, "created_today", lambda *a, **k: (500, {}))
    monkeypatch.setattr(cp, "run_priority_cycle",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should not walk")))
    cp.main()
    assert "plan_already_met" in capsys.readouterr().out


def test_main_passes_plan_to_cycle(tmp_path, monkeypatch):
    import upload_to_ozon

    monkeypatch.chdir(tmp_path)
    daily_plan.save_plan({"default": {"total": 300, "allocations": {"M&S": {"Socks": 50}},
                                      "fill_remainder": False}, "overrides": {}})
    monkeypatch.setattr(upload_to_ozon, "check_quota",
                        lambda: {"daily_create": {"limit": 2000, "usage": 1900}})
    monkeypatch.setattr(daily_plan, "created_today", lambda *a, **k: (20, {("M&S", "Socks"): 10}))
    got = {}
    monkeypatch.setattr(cp, "run_priority_cycle", lambda **k: got.update(k) or {})
    cp.main()
    assert got == {"target_new_products": 100, "per_category_target": {("M&S", "Socks"): 40},
                   "fill_remainder": False}
