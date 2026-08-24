"""
Maps M&S product attributes onto Ozon's controlled dictionaries
(category: Clothing > Underwear > Underwear Trunks/Panties).
"""
import difflib
import json

from ozon_client import call

CATEGORY_ID = 200001517
TYPE_ID_PANTIES = 93238          # "Underwear Trunks/Panties" — single-item briefs/knickers
TYPE_ID_PANTIES_SET = 970617577  # "Underwear Trunks/Panties Set" — multi-packs ("3'lü", "5'li")

CATEGORY_ID_CLOTHING = 200000933
TYPE_ID_TANK_TOP = 93150         # "Tank Top" — matches M&S "atlet"

ATTR_SIZE = 4295
ATTR_GENDER = 9163
ATTR_COLOR = 10096
ATTR_BRAND = 31

GENDER_FEMALE_ID = 22881  # confirmed against the live dictionary

# Verified via /v1/description-category/attribute/values/search — several near-duplicate
# brand entries exist (e.g. "Marks&Spencer", "Marks & Spenser" misspelling), so this is
# hardcoded to the one confirmed-correct entry rather than left to fuzzy matching.
BRAND_MARKS_AND_SPENCER_ID = 971843743

# UK -> RU women's underwear size, as given by the business (verified against
# Ozon's live size dictionary — every RU value below has a matching entry).
UK_TO_RU_SIZE = {
    "6": "40", "8": "42", "10": "44", "12": "46", "14": "48",
    "16": "50", "18": "52", "20": "54", "22": "56", "24": "58", "26": "60", "28": "62",
}

# Letter size -> EU/RU size, per M&S's official top-garment size chart (2026-08-25),
# using the LOWER bound of each letter's EU range (business decision — conservative,
# avoids overstating size to the buyer). Verified against Ozon's live size dictionary.
LETTER_TO_RU_SIZE = {
    "XS": "34", "S": "36", "M": "40", "L": "44", "XL": "48", "XXL": "52",
}

# Manual overrides for M&S color names that don't have a clean automatic match
# against Ozon's dictionary (checked once against the live 200-value list).
COLOR_OVERRIDES = {
    "OPALINE": "white",       # opaline is a milky-white tone; closest available dictionary value
    "ROSE QUARTZ": "pink",    # rose quartz is a pale pink; closest available dictionary value
}

_size_dict_cache = None
_color_dict_cache = None


def _load_size_dict():
    global _size_dict_cache
    if _size_dict_cache is None:
        resp = call("/v1/description-category/attribute/values", {
            "description_category_id": CATEGORY_ID,
            "type_id": TYPE_ID_PANTIES,
            "attribute_id": ATTR_SIZE,
            "language": "EN",
            "limit": 200,
        })
        _size_dict_cache = {v["value"]: v["id"] for v in resp.get("result", [])}
    return _size_dict_cache


def _load_color_dict():
    global _color_dict_cache
    if _color_dict_cache is None:
        resp = call("/v1/description-category/attribute/values", {
            "description_category_id": CATEGORY_ID,
            "type_id": TYPE_ID_PANTIES,
            "attribute_id": ATTR_COLOR,
            "language": "EN",
            "limit": 200,
        })
        _color_dict_cache = {v["value"]: v["id"] for v in resp.get("result", [])}
    return _color_dict_cache


def extract_uk_size(size_label):
    """'34 (UK 6)' -> '6'. Returns None if no numeric UK size found."""
    if not size_label:
        return None
    import re
    m = re.search(r"UK\s*(\d+)", size_label)
    return m.group(1) if m else None


def extract_letter_size(size_label):
    """'S (UK S)' -> 'S'. Returns None if no letter size found."""
    if not size_label:
        return None
    import re
    m = re.search(r"UK\s*(XXL|XS|S|M|L|XL)\b", size_label)
    return m.group(1) if m else None


def map_size_to_ozon(size_label):
    """Returns (dictionary_value_id, ru_size_str, warning_or_None). Handles both
    numeric UK sizes ('34 (UK 6)') and letter sizes ('S (UK S)')."""
    uk = extract_uk_size(size_label)
    ru_size = UK_TO_RU_SIZE.get(uk) if uk else None

    if not ru_size:
        letter = extract_letter_size(size_label)
        ru_size = LETTER_TO_RU_SIZE.get(letter) if letter else None
        if not ru_size:
            return None, None, f"Could not extract a known UK numeric or letter size from label {size_label!r}"

    size_dict = _load_size_dict()
    value_id = size_dict.get(ru_size)
    if not value_id:
        return None, ru_size, f"RU size {ru_size!r} not found in Ozon's live size dictionary"

    return value_id, ru_size, None


def map_color_to_ozon(mands_color):
    """Returns (dictionary_value_id, matched_ozon_value, warning_or_None).
    Uses manual overrides first, then closest-match fuzzy matching as a fallback."""
    if not mands_color:
        return None, None, "No color value provided"

    color_dict = _load_color_dict()
    color_upper = mands_color.strip().upper()

    if color_upper in COLOR_OVERRIDES:
        target = COLOR_OVERRIDES[color_upper]
        value_id = color_dict.get(target)
        return value_id, target, None if value_id else f"Override target {target!r} not in dictionary"

    # Try each word in the M&S color name against dictionary values (case-insensitive),
    # e.g. "PINK MIX" -> "pink" is a direct substring/word match.
    lower_words = color_upper.lower().split()
    for dict_value in color_dict:
        if dict_value.lower() in lower_words:
            return color_dict[dict_value], dict_value, None

    # Fall back to fuzzy string matching across the full color name.
    candidates = list(color_dict.keys())
    best = difflib.get_close_matches(color_upper.lower(), candidates, n=1, cutoff=0.3)
    if best:
        matched = best[0]
        return color_dict[matched], matched, f"Fuzzy-matched {mands_color!r} -> {matched!r} (no exact/override match)"

    return None, None, f"No match found for color {mands_color!r} — needs a manual override"


def resolve_category_and_type(name, is_set_hint):
    """Determines (description_category_id, type_id) from the M&S product name.
    Keyword-based: 'kulot'/'külot' = underwear briefs, 'atlet' = tank top.
    Returns None if the product doesn't match a known category — caller should
    skip rather than guess, since an unmapped category means unknown required fields."""
    lower = (name or "").lower()
    if "kulot" in lower or "külot" in lower or "tanga" in lower:
        return CATEGORY_ID, (TYPE_ID_PANTIES_SET if is_set_hint else TYPE_ID_PANTIES)
    if "atlet" in lower:
        return CATEGORY_ID_CLOTHING, TYPE_ID_TANK_TOP
    return None, None


def log_mapping_decision(log_path, sku, field, input_value, output_value, warning):
    with open(log_path, "a", encoding="utf-8") as f:
        entry = {
            "sku": sku, "field": field, "input": input_value,
            "output": output_value, "warning": warning,
        }
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
