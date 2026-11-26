"""Offline tests for hm_catalog.py (H&M -> Ozon mapping) -- Ozon dictionaries are stubbed."""
import pytest

import hm_catalog as hc
from ozon_mapping import TYPE_ID_DRESS, TYPE_ID_SOCKS


@pytest.fixture(autouse=True)
def dicts(monkeypatch):
    sizes = {str(n): 1000 + n for n in range(32, 66, 2)}
    sizes["universal"] = 35646
    monkeypatch.setattr(hc, "_load_size_dict", lambda: sizes)
    monkeypatch.setattr(hc, "_load_color_dict", lambda: {"black": 1, "dark grey": 2, "red": 3, "dark brown": 4})


def test_colour_strips_patterns_and_takes_base():
    assert hc.hm_color("Siyah") == "black"
    assert hc.hm_color("Koyu gri kırçıllı") == "dark grey"
    assert hc.hm_color("Kırmızı/Desenli") == "red"
    assert hc.hm_color("Koyu kahverengi/Leopar desenli") == "dark brown"
    assert hc.hm_color("Gri-bej") is None          # compound -> skip, don't guess
    assert hc.hm_color("Leopar desenli") is None   # pattern only
    assert hc.hm_color(None) is None


def test_sizes_letter_numeric_socks():
    assert hc.ru_size("XS") == (1042, "42", "XS")
    assert hc.ru_size("M") == (1046, "46", "M")
    assert hc.ru_size("36") == (1042, "42", "36")   # EU 36 -> RU 42, same chart as M&S
    assert hc.ru_size("35") == (None, None, None)   # odd EU -> skip
    assert hc.ru_size("32B") == (None, None, None)  # bra size -> skip
    assert hc.ru_size("M/L") == (None, None, None)
    assert hc.ru_size("37-39", is_socks=True) == (35646, "universal", "3739")


def test_classify_and_tights_excluded():
    assert hc.classify("Lastik Örgülü Elbise")[1] == TYPE_ID_DRESS
    assert hc.classify("5'li Paket Çorap")[1] == TYPE_ID_SOCKS
    assert hc.classify("20 Denye Külotlu Çorap") == (None, None)
    assert hc.classify("Sütyen")[1] is None
    assert hc.classify("Drapeli Tişört Elbise")[1] == TYPE_ID_DRESS  # a t-shirt DRESS is a dress


@pytest.mark.parametrize("title,expected", [
    ("Saten Slip Etek", "Skirt"), ("MAMA Dantel Şeritli Slip Etek", "Skirt"),
    ("Kloş Etekli Midi Elbise", "Dress"), ("Sweatshirt Elbise", "Dress"),
    ("Etek Ucu Balon Kesimli Ceket", "Jacket"), ("Etek Ucu Balon Kesimli Üst", "Top"),
    ("Bluzon Ceket", "Jacket"), ("Saten Bluz", "Blouse"),
    ("Pijama Üstü", "Pajama"), ("Atletli Pijama Takımı", "Pajama"),
    ("3'lü Hipster Külot", "Underwear Set"), ("Brazilian Külot", "Underwear"), ("Slip Elbise", "Dress"),
    ("Pantolon Çorabı", "Socks"), ("Takım Elbise Ceketi", "Jacket"), ("Triko Tişört", "T-Shirt"),
])
def test_last_garment_word_wins(title, expected):
    from daily_plan import CATEGORY_LABELS
    assert CATEGORY_LABELS.get(hc.classify(title)[1]) == expected


def test_tights_skipped_for_ms_too():
    from ozon_mapping import resolve_category_and_type
    assert resolve_category_and_type("3'lü 15 Denye Külotlu Çorap Seti", True) == (None, None)


def _hit(**kw):
    h = {"articleCode": "1375249001", "title": "Lastik Örgülü Elbise", "category": "ladies_dresses",
         "productColor": {"colorName": "Siyah"}, "prices": [{"price": 2299}],
         "sizes": [{"name": "XS", "stock": 2}, {"name": "S", "stock": 1}, {"name": "M", "stock": 0},
                   {"name": "32B", "stock": 2}]}
    h.update(kw)
    return h


def test_summarize_and_skip_rules():
    rec = hc.summarize_hit(_hit())
    assert rec["ozon_color"] == "black" and rec["type_id"] == TYPE_ID_DRESS and rec["price_try"] == 2299
    sell = hc.sellable_sizes(rec)
    assert [(s["token"], s["stock"]) for s in sell] == [("XS", 5), ("S", 1)]  # 2 -> 5, 1 -> 1, 0 dropped
    assert hc.skip_reason(rec, set()) is None
    assert hc.skip_reason(rec, {"1375249001"}) == "already on Ozon"
    assert hc.skip_reason(hc.summarize_hit(_hit(productColor={"colorName": "Gri-bej"})), set()) == "colour not mapped"
    assert hc.skip_reason(hc.summarize_hit(_hit(title="Sütyen")), set()) == "category not mapped"
    assert hc.skip_reason(hc.summarize_hit(_hit(sizes=[{"name": "XS", "stock": 0}])), set()) == "no in-stock mappable size"


def test_build_items_offer_ids_and_attributes():
    rec = hc.summarize_hit(_hit())
    detail = {"variation": {"images": [{"baseUrl": "https://image.hm.com/a.jpg"}, {"baseUrl": "https://image.hm.com/a.jpg"},
                                       {"baseUrl": "//bad"}]}}
    tr = {"name": "Платье", "description": "Описание", "care_text": "Уход", "material_composition": "Состав: хлопок",
          "hashtags": "#a #b #c #d #e", "material_id": None}
    items = hc.build_items(rec, detail, tr, 0.083, {"weight": 100, "depth": 15, "width": 12, "height": 2})
    assert [i["offer_id"] for i in items] == ["HM-1375249001-XS", "HM-1375249001-S"]
    it = items[0]
    assert it["name"] == "H&M Платье" and it["price"] == str(round(2299 * 0.083, 2))
    assert it["images"] == ["https://image.hm.com/a.jpg"] and it["_stock"] == 5
    attrs = {a["id"]: a["values"][0] for a in it["attributes"]}
    assert attrs[31] == {"dictionary_value_id": hc.BRAND_HM_ID}
    assert attrs[8292] == {"value": "HM-1375249"}
    assert hc.build_items(rec, {"variation": {"images": []}}, tr, 0.083, {"weight": 1, "depth": 1, "width": 1, "height": 1}) == []
