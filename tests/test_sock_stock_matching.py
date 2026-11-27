"""Sock offer_ids end in the whole size range ("-3-6Yaş", "-355-38"); found 2026-11-27 that the
nightly M&S stock job cut the token at the last '-' and zeroed every sock offer."""
import pandas as pd

import refresh_live_stock as rls
import update_stocks as us

ROWS = [
    {"ms_article_code": "T71004078C", "parent_sku": "P1", "color": "PINK MIX", "name": "4'lü Çorap",
     "size_label": "3-6 Yaş (UK 8+-12)", "stock_count": 1},
    {"ms_article_code": "T71004078C", "parent_sku": "P1", "color": "PINK MIX", "name": "4'lü Çorap",
     "size_label": "11-14 Yaş (UK 4-7)", "stock_count": 3},
    {"ms_article_code": "T71004078C", "parent_sku": "P1", "color": "PINK MIX", "name": "4'lü Çorap",
     "size_label": "35.5-38 (UK 3-5)", "stock_count": 7},
]


def test_live_refresh_matches_sock_range_tokens(monkeypatch):
    monkeypatch.setattr(rls, "extract_product", lambda url: ROWS)
    monkeypatch.setattr(rls, "verify_url_matches_offer_id", lambda *a: True)
    ids = ["MS-T71004078C-PINKMIX-3-6Yaş", "MS-T71004078C-PINKMIX-11-14Yaş", "MS-T71004078C-PINKMIX-355-38",
           "MS-T71004078C-PINKMIX-7-10Yaş"]
    updates, unmatched, wrong, _w = rls.build_stock_updates_for_url("u", ids)
    got = {u["offer_id"]: u["stock"] for u in updates}
    assert got == {ids[0]: 1, ids[1]: 3, ids[2]: 7, ids[3]: 0}
    assert unmatched == [ids[3]] and wrong == []


def test_csv_builder_uses_sock_tokens():
    got = {u["offer_id"]: u["stock"] for u in us.build_stock_updates(pd.DataFrame(ROWS))}
    assert got["MS-T71004078C-PINKMIX-3-6Yaş"] == 1 and got["MS-T71004078C-PINKMIX-355-38"] == 7
