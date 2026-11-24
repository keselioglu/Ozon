"""Offline tests for content_media.py -- no network, no real data files."""
import json

import pytest

import content_media as cm


def _record(offer_id="MS-T123-BLACK-40"):
    return {
        "offer_id": offer_id,
        "name": "Женские трусы",
        "description_category_id": 200000933,
        "type_id": 93244,
        "attributes": [
            {"id": 9163, "values": [{"dictionary_value_id": 22880, "value": "Женский"}]},
            {"id": 10096, "values": [{"dictionary_value_id": 61574, "value": "черный"}]},
            {"id": 23171, "values": [{"value": "#old"}]},
            {"id": 4295, "values": [{"dictionary_value_id": 1, "value": "46"}]},
            {"id": 21845, "values": [{"value": "https://cover"}]},
        ],
        "images": ["https://ms/real1.jpg", "https://ms/real2.jpg"],
        "primary_image": "https://ms/real1.jpg",
        "weight": 100, "weight_unit": "g",
        "depth": 10, "width": 20, "height": 30, "dimension_unit": "mm",
    }


PRICE = {"price": "12.50", "currency_code": "USD", "vat": "0"}


# --- build_merged_import_item ------------------------------------------------

def test_merged_item_keeps_untouched_attributes_and_price():
    rec = _record()
    item = cm.build_merged_import_item(rec, PRICE, attr_overrides={23171: "#new"})

    assert item["price"] == "12.50"
    assert item["currency_code"] == "USD"
    assert item["vat"] == "0"
    by_id = {a["id"]: a for a in item["attributes"]}
    for untouched in (9163, 10096, 4295, 21845):
        orig = next(a for a in rec["attributes"] if a["id"] == untouched)
        assert by_id[untouched] == orig
    assert by_id[23171] == {"id": 23171, "values": [{"value": "#new"}]}
    assert [a["id"] for a in item["attributes"]] == [9163, 10096, 23171, 4295, 21845]
    for field in ("name", "description_category_id", "type_id", "weight", "weight_unit",
                  "depth", "width", "height", "dimension_unit"):
        assert item[field] == rec[field]
    assert item["images"] == rec["images"]
    assert item["primary_image"] == rec["primary_image"]
    # live record itself is not mutated
    assert rec["attributes"][2] == {"id": 23171, "values": [{"value": "#old"}]}


def test_merged_item_adds_new_and_removes_none_overrides():
    item = cm.build_merged_import_item(_record(), PRICE, attr_overrides={
        cm.ATTR_VIDEO_LINK: "https://r2/video.mp4",
        cm.ATTR_VIDEO_COVER: None,
        cm.ATTR_RICH_CONTENT: [{"value": "{}"}],
    })
    ids = [a["id"] for a in item["attributes"]]
    assert cm.ATTR_VIDEO_COVER not in ids
    assert ids[:4] == [9163, 10096, 23171, 4295]
    by_id = {a["id"]: a for a in item["attributes"]}
    assert by_id[cm.ATTR_VIDEO_LINK]["values"] == [{"value": "https://r2/video.mp4"}]
    assert by_id[cm.ATTR_RICH_CONTENT]["values"] == [{"value": "{}"}]


def test_merged_item_real_images_first_then_generated():
    real = ["https://ms/a.jpg", "https://ms/b.jpg", "https://ms/c.jpg"]
    gen = ["https://r2/zoom_1.jpg", "https://r2/zoom_2.jpg", "https://ms/a.jpg"]
    item = cm.build_merged_import_item(_record(), PRICE, images=real, generated_images=gen)
    assert item["images"] == real + ["https://r2/zoom_1.jpg", "https://r2/zoom_2.jpg"]
    assert item["primary_image"] == "https://ms/real1.jpg"

    # without explicit images, the live record's images are kept first
    item = cm.build_merged_import_item(_record(), PRICE, generated_images=["https://r2/zoom_1.jpg"])
    assert item["images"] == ["https://ms/real1.jpg", "https://ms/real2.jpg", "https://r2/zoom_1.jpg"]


def test_merged_item_requires_price_and_ms_prefix():
    with pytest.raises(ValueError):
        cm.build_merged_import_item(_record(), {"price": "", "currency_code": "USD", "vat": "0"})
    with pytest.raises(ValueError):
        cm.build_merged_import_item(_record("MAR-T123-BLACK-40"), PRICE)


# --- (article, color) -> offer_id ---------------------------------------------

def test_rebuild_offer_id_matches_build_sku():
    assert cm.rebuild_offer_id("T123", "Dark Navy", "40 (UK 12)") == "MS-T123-DARKNAVY-40"
    assert cm.rebuild_offer_id("T123", "Black", "M (UK M)") == "MS-T123-BLACK-M"
    assert cm.rebuild_offer_id("T123", "Black", "") is None
    assert cm.rebuild_offer_id("", "Black", "40 (UK 12)") is None


def test_offer_ids_for_group_rejects_legacy_prefix():
    live = {"MAR-T123-BLACK-40", "SML-T123-BLACK-42", "MS-T123-BLACK-44", "MS-T123-WHITE-40"}
    got = cm.offer_ids_for_group("T123", "Black", ["40 (UK 12)", "42 (UK 14)", "44 (UK 16)"], live)
    assert got == ["MS-T123-BLACK-44"]
    # another color's offer_id never matches
    assert "MS-T123-WHITE-40" not in got


def test_require_ms_offer_id():
    assert cm.require_ms_offer_id("MS-T1-BLACK-40") == "MS-T1-BLACK-40"
    for bad in ("MAR-T1-BLACK-40", "MARK-T1", "", None, "ms-T1"):
        with pytest.raises(ValueError):
            cm.require_ms_offer_id(bad)


def test_find_live_ms_offer_ids_filters_prefix_and_archived():
    def fake_call(path, params):
        assert path == "/v3/product/list"
        return {"result": {"items": [
            {"offer_id": "MS-A-BLACK-40"},
            {"offer_id": "MS-A-BLACK-42", "archived": True},
            {"offer_id": "MAR-A-BLACK-40"},
        ], "last_id": ""}}
    assert cm.find_live_ms_offer_ids(api_call=fake_call) == ["MS-A-BLACK-40"]


# --- rich content / size table ------------------------------------------------

TRANSLATION = {
    "name": "Женские трусы",
    "material_text": "Хлопок",
    "care_text": "Стирка при 30 градусах.",
}


def test_build_rich_content_valid_json_with_widgets_in_order():
    out = cm.build_rich_content("T123", TRANSLATION, ["https://ms/a.jpg", "https://ms/b.jpg"])
    data = json.loads(out)
    assert data["version"] == 0.3
    assert [w["widgetName"] for w in data["content"]] == ["raTextBlock", "raShowcase"]
    blocks = data["content"][1]["blocks"]
    assert [b["reverse"] for b in blocks] == [False, True]
    assert all(b["img"]["src"] == "https://ms/a.jpg" for b in blocks)


def test_build_rich_content_without_image_is_text_only():
    data = json.loads(cm.build_rich_content("T123", TRANSLATION, []))
    assert data["version"] == 0.3
    assert [w["widgetName"] for w in data["content"]] == ["raTextBlock", "raTextBlock"]


def test_build_rich_content_requires_translation():
    with pytest.raises(ValueError):
        cm.build_rich_content("T123", None, ["https://ms/a.jpg"])


def test_size_table_for_known_category_name_and_url(monkeypatch):
    # map_size_to_ozon reads Ozon's size dictionary -- seed the cache offline.
    import ozon_mapping
    monkeypatch.setattr(ozon_mapping, "_size_dict_cache",
                        {str(n): 1000 + n for n in range(30, 70)})
    for arg in ("kadin_ic_giyim", "Pamuklu Külot", None):
        url = "https://www.marksandspencer.com.tr/kadin-kulot-p_123" if arg is None else None
        out = cm.size_table_for(arg, url=url)
        assert out is not None
        data = json.loads(out)
        assert data["version"] == 0.1
        assert data["content"][0]["widgetName"] == "tcTable"
        assert data["content"][0]["table"]["body"]


def test_size_table_for_unmapped_category_returns_none():
    assert cm.size_table_for("Yün Kaban") is None
    assert cm.size_table_for("unknown_category") is None
    assert cm.size_table_for("") is None
    assert cm.size_table_for(None) is None


# --- r2 keys --------------------------------------------------------------------

def test_r2_object_key_for_keeps_existing_layout():
    assert cm.r2_object_key_for("generated_photos/T1_BLACK/zoom_1.jpg") == "extra_photos/T1_BLACK/zoom_1.jpg"
    assert cm.r2_object_key_for("generated_videos/T1_BLACK/video.mp4") == "videos/T1_BLACK/video.mp4"


# --- push_one_and_verify ------------------------------------------------------

def test_push_one_and_verify_trusts_reread_not_status(monkeypatch):
    monkeypatch.setattr(cm.time, "sleep", lambda s: None)
    reads = {"n": 0}

    def fake_call(path, params):
        if path == "/v3/product/import":
            assert len(params["items"]) == 1
            return {"result": {"task_id": 7}}
        if path == "/v1/product/import/info":
            return {"result": {"items": [{"status": "imported", "errors": []}]}}
        if path == "/v4/product/info/attributes":
            reads["n"] += 1
            # stale on the first re-read, updated on the second
            link = [] if reads["n"] == 1 else [{"value": "https://r2/v.mp4"}]
            return {"result": [{"offer_id": "MS-T1-BLACK-40", "images": [],
                                "attributes": [{"id": cm.ATTR_VIDEO_LINK, "values": link}]}]}
        raise AssertionError(path)

    item = cm.build_merged_import_item(_record("MS-T1-BLACK-40"), PRICE,
                                       attr_overrides={cm.ATTR_VIDEO_LINK: "https://r2/v.mp4"})
    res = cm.push_one_and_verify(item, cm.check_attr_present(cm.ATTR_VIDEO_LINK), api_call=fake_call)
    assert res["verified"] is True
    assert reads["n"] == 2
    assert res["status"] == "imported"

    reads["n"] = -100  # never updates
    res = cm.push_one_and_verify(item, [cm.check_attr_equals(cm.ATTR_VIDEO_LINK, "nope")],
                                 api_call=fake_call, attempts=2)
    assert res["verified"] is False


def test_push_one_and_verify_rejects_legacy_offer_id():
    with pytest.raises(ValueError):
        cm.push_one_and_verify({"offer_id": "MAR-T1"}, [lambda r: True], api_call=lambda *a: {})
