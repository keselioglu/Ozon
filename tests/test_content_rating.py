"""Offline tests for content_rating.py -- no network, no real data files."""
import os
import re

import pytest

import content_rating as cr


def _cond(key, fulfilled, cost=5):
    return {"key": key, "description": f"desc {key}", "fulfilled": fulfilled, "cost": cost}


FIXTURE = {
    "products": [
        {
            "sku": 1001,
            "rating": 62.5,
            "groups": [
                {
                    "key": "media", "name": "Медиа", "rating": 40, "weight": 25,
                    "improve_at_least": 2,
                    "conditions": [_cond("media_images_3", True), _cond("media_video", False, 10)],
                    "improve_attributes": [{"id": 21841, "name": "Видео"}],
                },
                {
                    "key": "important_attributes", "name": "Важные", "rating": 80, "weight": 30,
                    "improve_at_least": 1,
                    "conditions": [_cond("imp_attr_filled", False, 8)],
                    "improve_attributes": [{"id": 4496, "name": "Материал"}],
                },
                {
                    "key": "other_attributes", "name": "Прочие", "rating": 50, "weight": 25,
                    "improve_at_least": 3,
                    "conditions": [_cond("other_attr_filled", False, 3)],
                    "improve_attributes": [],
                },
                {
                    "key": "text", "name": "Текст", "rating": 100, "weight": 20,
                    "improve_at_least": 0,
                    "conditions": [_cond("text_rich", True)],
                    "improve_attributes": [],
                },
            ],
        },
        {"sku": "1002", "rating": 90, "groups": []},
        {"sku": 9999, "rating": 10, "groups": []},  # not requested -> ignored
    ]
}

SKU_TO_OFFER = {1001: "MS-A-1", 1002: "MS-A-2", 1003: "MS-A-3"}


def test_parse_all_four_groups():
    out = cr.parse_rating_response(FIXTURE, SKU_TO_OFFER)
    rec = out["MS-A-1"]
    assert rec["offer_id"] == "MS-A-1" and rec["sku"] == 1001 and rec["rating"] == 62.5
    assert [g["key"] for g in rec["groups"]] == ["media", "important_attributes", "other_attributes", "text"]
    media = rec["groups"][0]
    assert media == {
        "key": "media", "name": "Медиа", "rating": 40, "weight": 25, "improve_at_least": 2,
        "unmet_conditions": [{"key": "media_video", "description": "desc media_video", "cost": 10}],
        "improve_attributes": [{"id": 21841, "name": "Видео"}],
    }
    assert rec["groups"][3]["unmet_conditions"] == []


def test_missing_sku_and_unrequested_sku():
    out = cr.parse_rating_response(FIXTURE, SKU_TO_OFFER)
    assert set(out) == {"MS-A-1", "MS-A-2"}  # 1003 missing, 9999 ignored


def test_empty_groups_and_string_sku():
    rec = cr.parse_rating_response(FIXTURE, SKU_TO_OFFER)["MS-A-2"]
    assert rec["sku"] == 1002 and rec["groups"] == [] and rec["rating"] == 90


def test_empty_response():
    assert cr.parse_rating_response({}, SKU_TO_OFFER) == {}
    assert cr.parse_rating_response(None, SKU_TO_OFFER) == {}


def test_summarize_math():
    snap = cr.parse_rating_response(FIXTURE, SKU_TO_OFFER)
    snap["MS-B"] = {"offer_id": "MS-B", "sku": 1, "rating": 100, "groups": []}
    snap["MS-C"] = {"offer_id": "MS-C", "sku": 2, "rating": 0, "groups": []}
    snap["MS-D"] = {"offer_id": "MS-D", "sku": 3, "rating": 79.99, "groups": []}
    snap["MS-E"] = {"offer_id": "MS-E", "sku": 4, "rating": None, "groups": []}
    s = cr.summarize(snap, run_date="2026-11-24")
    assert s["date"] == "2026-11-24"
    assert s["product_count"] == 6
    assert s["rated_count"] == 5
    assert s["average_rating"] == pytest.approx(round((62.5 + 90 + 100 + 0 + 79.99) / 5, 2))
    assert s["below_80"] == 3  # 62.5, 0, 79.99
    assert s["histogram"] == {"0": 1, "10": 0, "20": 0, "30": 0, "40": 0,
                              "50": 0, "60": 1, "70": 1, "80": 0, "90": 2}
    assert s["unmet_condition_counts"] == {"imp_attr_filled": 1, "media_video": 1, "other_attr_filled": 1}
    assert cr.format_done_line(s) == "Done. 5 rated, avg 66.5, 3 below 80."


def test_summarize_empty():
    s = cr.summarize({}, run_date="2026-11-24")
    assert s["product_count"] == 0 and s["average_rating"] is None
    assert sum(s["histogram"].values()) == 0
    assert cr.format_done_line(s) == "Done. 0 rated, avg 0.0, 0 below 80."


def test_extract_sku():
    assert cr.extract_sku({"sku": 5}) == 5
    assert cr.extract_sku({"sku": 0, "sources": [{"sku": 0}, {"sku": "77"}]}) == 77
    assert cr.extract_sku({}) is None


def test_no_write_endpoints_in_source():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content_rating.py")
    src = open(path, encoding="utf-8").read()
    endpoints = re.findall(r'"(/v\d+/[a-z0-9_/\-]+)"', src)
    assert set(endpoints) == {"/v3/product/list", "/v3/product/info/list", "/v1/product/rating-by-sku"}
    assert not re.search(r"/import|/update|/deactivate|/archive", src)
