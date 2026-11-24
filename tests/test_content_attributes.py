"""Offline tests for content_attributes.py -- mocked Anthropic client and
Ozon API, temp cache files, no network."""
import json
from types import SimpleNamespace

import pytest

import content_attributes as ca

TYPE_ID = 93244

SCHEMA_RESPONSE = {"result": [
    {"id": 4495, "name": "Сезон", "type": "String", "is_collection": False, "dictionary_id": 1},
    {"id": 9390, "name": "Стиль", "type": "String", "is_collection": True, "dictionary_id": 2},
    {"id": 22336, "name": "Особенности модели", "type": "String", "is_collection": False, "dictionary_id": 0},
    {"id": 4604, "name": "Состав материала", "type": "String", "is_collection": False, "dictionary_id": 0},
    {"id": 4389, "name": "Страна-изготовитель", "type": "String", "is_collection": True, "dictionary_id": 3},
    {"id": 10096, "name": "Цвет товара", "type": "String", "is_collection": True, "dictionary_id": 4},
    {"id": 4496, "name": "Материал", "type": "String", "is_collection": True, "dictionary_id": 5},
    {"id": 9163, "name": "Пол", "type": "String", "is_collection": False, "dictionary_id": 6},
]}

VALUES = {
    4495: [{"id": 1001, "value": "Демисезон"}, {"id": 1002, "value": "Лето"}],
    9390: [{"id": 2001, "value": "Повседневный"}, {"id": 2002, "value": "Классический"}],
    4389: [{"id": 3001, "value": "Турция"}],
    10096: [{"id": 61574, "value": "черный"}],
    4496: [{"id": 62174, "value": "Хлопок"}],
    9163: [{"id": 22880, "value": "Женский"}],
}

ALL_IDS = [4495, 9390, 22336, 4604, 4389, 10096, 4496, 9163, 31, 4191, 23171, 4295, 8292, 8229]


class FakeOzon:
    def __init__(self):
        self.calls = []

    def __call__(self, path, payload=None):
        self.calls.append((path, payload))
        if path == "/v1/description-category/attribute":
            return SCHEMA_RESPONSE
        if path == "/v1/description-category/attribute/values":
            return {"result": VALUES.get(payload["attribute_id"], []), "has_next": False}
        raise AssertionError(f"unexpected Ozon call {path}")


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def _record(extra_attrs=None):
    return {
        "offer_id": "MS-T123-BLACK-40",
        "name": "Женская футболка",
        "description_category_id": 200000933,
        "type_id": TYPE_ID,
        "attributes": [
            {"id": 9163, "values": [{"dictionary_value_id": 22880, "value": "Женский"}]},
            {"id": 4191, "values": [{"value": "Женская хлопковая футболка свободного кроя."}]},
        ] + list(extra_attrs or []),
    }


@pytest.fixture
def files(tmp_path):
    return {
        "schema_cache": str(tmp_path / "schema.json"),
        "values_cache": str(tmp_path / "values.json"),
        "suggestions_file": str(tmp_path / "suggestions.jsonl"),
    }


def _run(client, files, ids=ALL_IDS, record=None, ozon=None, **kw):
    return ca.suggest_attributes(
        "T123", "BLACK", record or _record(), ids, client=client, api_call=ozon or FakeOzon(),
        translation={"name": "Футболка", "description": "Хлопковая футболка."},
        source={"name": "Saf Pamuklu Tişört", "description": "", "specs": {}},
        **files, **kw)


def _answer(*attrs, rich=None):
    return {"attributes": list(attrs), "rich_description": rich}


def _dict(attr_id, *ids):
    return {"id": attr_id, "dictionary_value_ids": list(ids), "value": None}


def _text(attr_id, value):
    return {"id": attr_id, "dictionary_value_ids": [], "value": value}


def test_valid_dictionary_and_free_text_values_kept(files):
    client = FakeClient(_answer(_dict(4495, 1001), _dict(9390, 2001, 2002),
                                _text(22336, "Свободный крой")))
    out = _run(client, files)
    assert out == {
        4495: [{"dictionary_value_id": 1001, "value": "Демисезон"}],
        9390: [{"dictionary_value_id": 2001, "value": "Повседневный"},
               {"dictionary_value_id": 2002, "value": "Классический"}],
        22336: [{"value": "Свободный крой"}],
    }


def test_dictionary_id_not_in_cached_values_is_dropped(files):
    client = FakeClient(_answer(_dict(4495, 999999), _dict(9390, 2001, 424242)))
    out = _run(client, files)
    assert 4495 not in out
    assert out[9390] == [{"dictionary_value_id": 2001, "value": "Повседневный"}]


def test_single_value_attribute_with_two_ids_is_ambiguous(files):
    out = _run(FakeClient(_answer(_dict(4495, 1001, 1002))), files)
    assert out == {}


def test_protected_ids_never_returned_or_asked(files):
    client = FakeClient(_answer(*[_dict(i, v["id"]) for i, vals in VALUES.items() for v in vals],
                                _text(31, "Бренд"), _text(4191, "Описание"), _text(23171, "#тег")))
    out = _run(client, files)
    assert not (set(out) & ca.PROTECTED_ATTR_IDS)
    prompt = client.calls[0]["messages"][0]["content"]
    asked = json.loads(prompt.split("Attributes to consider:\n", 1)[1].split("\n\nrich_description", 1)[0])
    asked_ids = {a["id"] for a in asked}
    assert not (asked_ids & ca.PROTECTED_ATTR_IDS)
    assert 4389 not in asked_ids  # country of origin is never asked
    assert 4389 not in out


@pytest.mark.parametrize("answer", [
    _answer(),
    _answer(_text(22336, "")),
    _answer(_text(22336, "unknown")),
    _answer(_text(22336, "Неизвестно")),
    _answer(_text(22336, None)),
    _answer(_dict(4495)),
    "not json",
    "",
])
def test_empty_or_unknown_answer_gives_no_attribute(files, answer):
    assert _run(FakeClient(answer), files) == {}


def test_cached_suggestion_is_reused_without_calling_model(files):
    first = FakeClient(_answer(_dict(4495, 1002)))
    ozon = FakeOzon()
    out1 = _run(first, files, ozon=ozon)
    assert len(first.calls) == 1

    second = FakeClient(_answer(_dict(4495, 1001)))
    ozon2 = FakeOzon()
    out2 = _run(second, files, ozon=ozon2)
    assert second.calls == []
    assert ozon2.calls == []  # schema and dictionary caches reused too
    assert out1 == out2 == {4495: [{"dictionary_value_id": 1002, "value": "Лето"}]}


def test_cache_only_asks_for_new_attributes(files):
    _run(FakeClient(_answer(_dict(4495, 1002))), files, ids=[4495])
    client = FakeClient(_answer(_dict(9390, 2001)))
    out = _run(client, files, ids=[4495, 9390])
    assert len(client.calls) == 1
    prompt = client.calls[0]["messages"][0]["content"]
    assert '"id": 9390' in prompt and '"id": 4495' not in prompt
    assert set(out) == {4495, 9390}


def test_attribute_already_filled_on_record_is_skipped(files):
    rec = _record([{"id": 4495, "values": [{"dictionary_value_id": 1001, "value": "Демисезон"}]}])
    client = FakeClient(_answer(_dict(4495, 1002)))
    assert _run(client, files, ids=[4495], record=rec) == {}
    assert client.calls == []


@pytest.mark.parametrize("text", ["95% хлопок, 5% эластан", "Хлопок 100 %", "хлопок 95,5%"])
def test_validator_rejects_percentage_in_composition(text):
    attr = {"id": 4604, "name": "Состав материала"}
    assert ca.is_composition_attribute(attr)
    assert ca.validate_free_text(attr, text) is None
    assert ca.validate_free_text(attr, "Хлопок, эластан") == "Хлопок, эластан"


def test_composition_percentage_dropped_end_to_end(files):
    out = _run(FakeClient(_answer(_text(4604, "95% хлопок, 5% эластан"))), files)
    assert out == {}


@pytest.mark.parametrize("text", ["Длина 70 см", "Made in Turkey", "Гарантия 1 год",
                                  "Подробнее на https://x.ru"])
def test_validator_rejects_invented_facts(text):
    assert ca.validate_free_text({"id": 22336, "name": "Особенности модели"}, text) is None


def test_forbidden_attribute_names():
    assert ca.is_forbidden_attribute({"name": "Страна-изготовитель"})
    assert ca.is_forbidden_attribute({"name": "Рост модели на фото"})
    assert not ca.is_forbidden_attribute({"name": "Сезон"})
    assert not ca.is_forbidden_attribute({"name": "Возрастная группа"})


def test_values_pagination_uses_last_value_id(tmp_path):
    pages = [
        {"result": [{"id": i, "value": f"v{i}"} for i in range(1, 201)], "has_next": True},
        {"result": [{"id": 500, "value": "last"}], "has_next": False},
    ]
    seen = []

    def api(path, payload):
        seen.append(payload)
        return pages[len(seen) - 1]

    entry = ca.get_dictionary_values(TYPE_ID, 4495, api_call=api, cache_file=str(tmp_path / "v.json"))
    assert len(entry["values"]) == 201 and not entry["too_large"]
    assert [p["last_value_id"] for p in seen] == [0, 200]
    assert all(p["limit"] <= 200 for p in seen)


def test_rating_helpers():
    rating = {"groups": [
        {"key": "media", "improve_attributes": [{"id": 21841}], "unmet_conditions": [{"key": "x"}]},
        {"key": "important_attributes", "improve_attributes": [{"id": 4495}, {"id": 4496}]},
        {"key": "other_attributes", "improve_attributes": [{"id": 9390}, {"id": 4495}]},
        {"key": "text", "improve_attributes": [], "unmet_conditions": [{"key": "text_rich"}]},
    ]}
    assert ca.improve_attribute_ids_from_rating(rating) == [4495, 9390]
    assert ca.text_group_unmet(rating)
    assert not ca.text_group_unmet({"groups": [{"key": "text", "unmet_conditions": []}]})


def test_rich_text_validated(files):
    good = ("Женская футболка из мягкого хлопкового трикотажа свободного кроя. "
            "Круглый вырез горловины и короткие рукава делают модель удобной на каждый день. "
            "Однотонная расцветка легко сочетается с джинсами, юбками и брюками. "
            "Материал приятен к телу и хорошо подходит для теплого сезона. "
            "Изделие легко стирается в стиральной машине при низкой температуре.")
    client = FakeClient(_answer(rich=good))
    out = ca.suggest_content("T123", "BLACK", _record(), [], want_rich_text=True, client=client,
                             api_call=FakeOzon(), translation={"name": "x"}, source=None, **files)
    assert out["rich_text"] == good
    assert ca.validate_rich_text(good + " Состав: 95% хлопок.") is None
    assert ca.validate_rich_text(good + " #футболка") is None
    assert ca.validate_rich_text("Коротко.") is None
