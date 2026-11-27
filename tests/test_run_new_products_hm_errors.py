"""A broken H&M page must not kill the daily run (2026-11-27 03:00 crash)."""
import sys
import types

import hm_catalog
import run_new_products as rnp
from hm_site import HMError
from ozon_mapping import TYPE_ID_PANTIES


class FakeHM:
    def __init__(self, broken_paths=(), broken_products=()):
        self.broken_paths, self.broken_products, self.calls = broken_paths, broken_products, []

    def listing(self, path, page):
        self.calls.append((path, page))
        if any(b in path for b in self.broken_paths) and page == 2:
            raise HMError(f"{path}?page=2: AbortError")
        return [{"article": f"{abs(hash(path)) % 10**6:06d}{page}{i:03d}"} for i in range(2)], 3

    def product(self, article):
        if article in self.broken_products:
            raise HMError("product page aborted")
        return {"sizes": 1}


def _setup(monkeypatch):
    monkeypatch.setitem(sys.modules, "hm_inventory", types.SimpleNamespace(live_hm_articles=lambda: set()))
    monkeypatch.setattr(hm_catalog, "summarize_hit", lambda h: {"article": h["article"], "type_id": TYPE_ID_PANTIES,
                                                                 "price_try": 100})
    monkeypatch.setattr(hm_catalog, "skip_reason", lambda rec, live: None)
    monkeypatch.setattr(hm_catalog, "sellable_sizes", lambda rec: [1])
    monkeypatch.setattr(rnp, "needed", lambda *a: 100)
    monkeypatch.setattr(rnp, "price_ok", lambda *a: True)


def test_unreadable_listing_page_skips_section_not_run(monkeypatch):
    _setup(monkeypatch)
    hm = FakeHM(broken_paths=("ic-camasir",))
    report = {"hm_found": {}}
    out = rnp.hm_candidates(["Underwear"], {}, {}, report, hm)
    assert len(out["Underwear"]) == 2                       # page 1 kept, rest of section skipped
    assert report["hm_skips"]["listing page unreadable"] == 1


class DeepHM(FakeHM):
    """30-page sections whose first 6 pages are already on Ozon (test run 2026-11-27 came up 96/100)."""
    def listing(self, path, page):
        self.calls.append((path, page))
        return [{"article": f"{abs(hash(path)) % 10**4:04d}{page:03d}{i:03d}", "old": page <= 6}
                for i in range(2)], 30


def test_reads_past_page_6_while_short_and_stops_when_covered(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setattr(hm_catalog, "summarize_hit", lambda h: {"article": h["article"], "type_id": TYPE_ID_PANTIES,
                                                                 "price_try": 100, "old": h["old"]})
    monkeypatch.setattr(hm_catalog, "skip_reason", lambda rec, live: "already on Ozon" if rec["old"] else None)
    monkeypatch.setattr(rnp, "needed", lambda *a: 4)          # goal 4 * OVERSHOOT offers
    hm = DeepHM()
    report = {"hm_found": {}}
    out = rnp.hm_candidates(["Underwear"], {}, {}, report, hm)
    goal = 4 * rnp.OVERSHOOT
    assert len(out["Underwear"]) >= goal
    assert max(p for _s, p in hm.calls) <= 6 + -(-goal // 2) + 1     # stopped soon after covering it
    assert max(p for _s, p in hm.calls) > 6


def test_unreadable_product_page_is_skipped(monkeypatch):
    _setup(monkeypatch)
    hm = FakeHM()
    first = f"{abs(hash('/tr_tr/kadin/urune-gore-satin-al/ic-camasir.html')) % 10**6:06d}1000"
    hm.broken_products = (first,)
    report = {"hm_found": {}}
    out = rnp.hm_candidates(["Underwear"], {}, {}, report, hm)
    arts = [p["rec"]["article"] for p in out["Underwear"]]
    assert first not in arts and len(arts) == 5
