"""content_text.validate -- the rules for longer Ozon descriptions."""
import pytest

import content_text as ct

OK = "Мягкая юбка из вискозы, длина макси. " * 20


def test_valid_text_passes_and_is_trimmed():
    assert ct.validate("  " + OK + "  ") == OK.strip()


@pytest.mark.parametrize("bad, why", [
    ("Коротко.", "length"),
    (OK + " V-образный вырез", "Latin"),
    (OK + " хорошо드 струится", "not Cyrillic"),
    (OK + " смотрите на ozon", "Latin"),
    (OK + " на маркетплейсе", "forbidden"),
    ("Я" * 1600, "length"),
])
def test_invalid_texts_rejected(bad, why):
    with pytest.raises(ValueError, match=why):
        ct.validate(bad)
