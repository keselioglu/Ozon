"""Offline tests for improve_content_hm.py planning (no Ozon, no H&M site)."""
import content_media as cm
import improve_content_hm as hm

PHOTO_RATING = {"groups": [{"key": "media", "unmet_conditions": [
    {"key": "media_images_8+", "description": "Добавлено от 8 изображений"}]}]}


def test_hm_offer_ids_in_scope():
    assert cm.is_hm_offer_id("HM-0970819007-XS")
    assert not cm.is_hm_offer_id("HM1-0970819007-XS") and not cm.is_hm_offer_id("1-HM-0970819007")
    assert cm.require_content_offer_id("HM-1375249001-M") == "HM-1375249001-M"
    assert cm.require_content_offer_id("MS-T1-BLACK-40") == "MS-T1-BLACK-40"


def test_groups_by_article_with_min_rating():
    snap = {"HM-0970819007-XS": {"rating": 30}, "HM-0970819007-M": {"rating": 26},
            "HM-1375249001-S": {"rating": 70}, "HM1-1111111111-S": {"rating": 1}}
    groups = {g["article"]: g for g in hm.build_groups(snap)}
    assert set(groups) == {"0970819007", "1375249001"}
    assert groups["0970819007"]["min_rating"] == 26 and len(groups["0970819007"]["offer_ids"]) == 2
    ranked = hm.rank(list(groups.values()), {"HM-1375249001-S": 3}, set())
    assert ranked[0]["article"] == "1375249001"  # in stock first


def test_photos_are_real_hm_photos_only_when_hm_has_more():
    record = {"images": ["a", "b", "c"], "attributes": []}
    acts = hm.plan_offer(PHOTO_RATING, record, ["h1", "h2", "h3", "h4", "h5", "h6"], media_ok=False)
    assert acts["photos"] == 6 and "video" not in acts and "size_table" not in acts
    assert "photos" not in hm.plan_offer(PHOTO_RATING, record, ["h1", "h2"], media_ok=False)


def test_translation_from_live_record_when_none_on_file():
    record = {"name": "H&M Платье", "attributes": [
        {"id": 4191, "values": [{"value": "Описание"}]}, {"id": 4604, "values": [{"value": "Хлопок"}]}]}
    tr = hm.translation_for("0000000000", record)
    assert tr["name"] == "Платье" and tr["description"] == "Описание" and tr["material_composition"] == "Хлопок"
    assert hm.translation_for("0000000000", {"name": "H&M X", "attributes": []}) is None
