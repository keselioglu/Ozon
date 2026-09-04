"""
Maps M&S product attributes onto Ozon's controlled dictionaries
(category: Clothing > Underwear > Underwear Trunks/Panties).
"""
import difflib
import json
import re

from ozon_client import call

# Matches the M&S article code embedded in an offer_id regardless of naming
# convention/prefix (MS-, MAR-, SML-MAR-, SMLMS-, etc. all embed it the same
# way — confirmed live, see upload_to_ozon.py's duplicate-prevention check).
# Does NOT match legacy numeric-SKU offer_ids (e.g. "MS-10000000601019-S") —
# those are identified by parent_sku substring match instead, see
# extract_article_code_from_offer_id below.
ARTICLE_CODE_IN_OFFER_ID_RE = re.compile(r"T\d{5,9}[A-Z]{0,2}")


def extract_article_code_from_offer_id(offer_id):
    """Returns the M&S article code embedded in offer_id (e.g. 'T61008800T'),
    or None if this offer_id uses the legacy numeric-SKU convention instead
    (caller should fall back to matching on parent_sku in that case)."""
    m = ARTICLE_CODE_IN_OFFER_ID_RE.search(offer_id)
    return m.group(0) if m else None

CATEGORY_ID = 200001517
TYPE_ID_PANTIES = 93238          # "Underwear Trunks/Panties" — single-item briefs/knickers
TYPE_ID_PANTIES_SET = 970617577  # "Underwear Trunks/Panties Set" — multi-packs ("3'lü", "5'li")

CATEGORY_ID_CLOTHING = 200000933
TYPE_ID_TANK_TOP = 93150         # "Tank Top" — matches M&S "atlet"
TYPE_ID_PAJAMA = 93176           # "Pajama" — matches M&S "pijama" (confirmed live, 2026-09-01:
                                 # same required-attribute set as tank top -- 9163/10096/31/4295/8292/8229)
TYPE_ID_TSHIRT = 93244           # "T-Shirt" — matches M&S "t-shirt"/"tişört" (confirmed live,
                                 # 2026-09-04: same required-attribute set as tank top/pajama,
                                 # same RU size dictionary as existing clothing sizes -- added to
                                 # give category discovery more categories to fill daily quota
                                 # after every existing supported category was exhausted)

ATTR_SIZE = 4295
ATTR_GENDER = 9163
ATTR_COLOR = 10096
ATTR_BRAND = 31
ATTR_TYPE = 8229  # required for tank top (93150) and pajama (93176); its
                  # dictionary value id equals the type_id itself for both
                  # (confirmed live, 2026-09-01) -- not previously set in
                  # uploads despite being is_required=True, apparently
                  # non-enforced by Ozon's API for tank tops, but set
                  # explicitly here anyway now that pajama needs it too.

# Confirmed against the live dictionary for both 93238 (panties) and 93176
# (pajama) category/type pairs -- same IDs across at least these two types.
GENDER_FEMALE_ID = 22881
GENDER_MALE_ID = 22880

# Verified via /v1/description-category/attribute/values/search — several near-duplicate
# brand entries exist (e.g. "Marks&Spencer", "Marks & Spenser" misspelling), so this is
# hardcoded to the one confirmed-correct entry rather than left to fuzzy matching.
BRAND_MARKS_AND_SPENCER_ID = 971843743

# UK -> RU women's underwear size, as given by the business (verified against
# Ozon's live size dictionary — every RU value below has a matching entry).
# This is used ONLY for the Ozon size ATTRIBUTE (the dictionary value shown to
# buyers on the PDP) — never for the offer_id. See UK_TO_EU_SIZE below for that.
UK_TO_RU_SIZE = {
    "6": "40", "8": "42", "10": "44", "12": "46", "14": "48",
    "16": "50", "18": "52", "20": "54", "22": "56", "24": "58", "26": "60", "28": "62",
}

# UK -> EU women's size, the number M&S actually displays on its own page
# (e.g. label "40 (UK 12)" — "40" is the EU number, "12" is the UK number).
# This is the number embedded in every offer_id on this account, confirmed
# against live legacy listings (MAR-, SML-, MARKS-, MARK- all use this, not
# the UK->RU value) — see build_sku() in upload_to_ozon.py. Business-confirmed
# chart (2026-08-26): UK 6/8/10/12/14/16/18/20/22 -> EU 34/36/38/40/42/44/46/48/50,
# and separately UK 24 -> EU 52 -> RU 58 (confirmed via the 5XL row below).
# UK 26/28 (EU 54/56) remain an extrapolation of the chart's own +2-per-step
# pattern, not yet independently confirmed by the business.
UK_TO_EU_SIZE = {
    "6": "34", "8": "36", "10": "38", "12": "40", "14": "42",
    "16": "44", "18": "46", "20": "48", "22": "50", "24": "52", "26": "54", "28": "56",
}

# Letter size -> RU size, per the business's confirmed chart (2026-08-26):
# XXS/XS/S/M/L/XL/XXL/3XL/4XL/5XL -> EU 34/36/38/40/42/44/46/48/50/52
# -> RUS 40/42/44/46/48/50/52/54/56/58. This is the RU ATTRIBUTE value only —
# see LETTER_TO_EU_SIZE for the offer_id number. Confirmed: EU 58 (seen on
# some live offer_ids) is NOT part of this chart — those are stale/discontinued
# sizes, handled by archiving rather than a size mapping (see
# archive_by_offer_id_pattern.py / conversation history, 2026-08-26).
LETTER_TO_RU_SIZE = {
    "XXS": "40", "XS": "42", "S": "44", "M": "46", "L": "48",
    "XL": "50", "XXL": "52", "3XL": "54", "4XL": "56", "5XL": "58",
}

# Letter size -> EU size (the number shown on M&S's page / embedded in offer_ids),
# per the same business-confirmed chart.
LETTER_TO_EU_SIZE = {
    "XXS": "34", "XS": "36", "S": "38", "M": "40", "L": "42",
    "XL": "44", "XXL": "46", "3XL": "48", "4XL": "50", "5XL": "52",
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
    m = re.search(r"UK\s*(XXS|XXL|XS|S|M|L|XL|3XL|4XL|5XL)\b", size_label)
    return m.group(1) if m else None


def extract_eu_size(size_label):
    """'40 (UK 12)' -> '40' — the EU number M&S displays directly on the page,
    i.e. the leading number before the parenthesized UK size. Returns None if
    the label has no leading number (observed on real data: M&S sometimes omits
    it, e.g. label ' (UK 8)' with nothing before the space — a real gap on
    M&S's own page, not a parsing bug; callers should treat None as "can't
    resolve this size" rather than guessing)."""
    if not size_label:
        return None
    import re
    m = re.match(r"^\s*(\d+)\s*\(", size_label)
    return m.group(1) if m else None


def map_size_to_ozon(size_label):
    """Returns (dictionary_value_id, ru_size_str, warning_or_None) — the Ozon
    size ATTRIBUTE value, shown to buyers on the PDP. Handles both numeric UK
    sizes ('34 (UK 6)') and letter sizes ('S (UK S)'). NOT for building the
    offer_id — see map_size_to_eu for that."""
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


def is_letter_only_size_label(size_label):
    """True when M&S's own label is letter-to-letter, e.g. 'M (UK M)' —
    NOT a numeric UK size in parentheses (e.g. 'M (UK 12)', which DOES have
    a real EU equivalent via the letter->EU chart). Confirmed live,
    2026-09-03 (business report on MS-T14002558F-BLUEMIX-*): M&S shows some
    products with no numeric size at all anywhere on the page (label exactly
    'M (UK M)', 'L (UK L)', etc.) -- for these there is no real EU number to
    convert to, and fabricating one via LETTER_TO_EU_SIZE produced a wrong
    offer_id size (e.g. '-42' for a product M&S only ever calls 'M'),
    confirmed against products.csv: 296 distinct articles / 3,566 size rows
    use this exact pattern, not a rare edge case."""
    if not size_label:
        return False
    letter = extract_letter_size(size_label)
    if not letter:
        return False
    return bool(re.fullmatch(rf"{letter}\s*\(UK\s*{letter}\)", size_label.strip()))


def map_size_to_eu(size_label):
    """Returns (eu_size_str, warning_or_None) — the EU number (or, for
    genuinely letter-only products, the letter itself) embedded in every
    offer_id on this account.

    Order matters and is deliberate:
      1. Letter-only label ('M (UK M)') -> the letter verbatim. M&S shows NO
         numeric size anywhere on the page for these -- there is nothing to
         convert to, so the offer_id must use 'M', not a fabricated EU
         number (business-confirmed, 2026-09-03; see
         is_letter_only_size_label docstring). Checked FIRST, before the
         leading-number extraction below, since a letter-only label has no
         leading number to find anyway, but ordering this first keeps the
         intent explicit rather than relying on that being a no-op.
      2. The label's own leading EU number ('40 (UK 12)' -> '40') — most
         direct and always correct when present.
      3. UK->EU chart, for labels that give a numeric UK size but omit the
         leading EU number.
      4. Letter->EU chart, for labels like 'S (UK 6)' -- a LETTER size that
         nonetheless has a real numeric UK equivalent in the parentheses,
         unlike case 1's letter-to-letter labels.
    Matches the business's confirmed size chart (2026-08-26) for cases 3/4."""
    if is_letter_only_size_label(size_label):
        return extract_letter_size(size_label), None

    eu_from_label = extract_eu_size(size_label)
    if eu_from_label:
        return eu_from_label, None

    uk = extract_uk_size(size_label)
    eu_size = UK_TO_EU_SIZE.get(uk) if uk else None
    if eu_size:
        return eu_size, None

    letter = extract_letter_size(size_label)
    eu_size = LETTER_TO_EU_SIZE.get(letter) if letter else None
    if eu_size:
        return eu_size, None

    return None, f"Could not resolve an EU size from label {size_label!r}"


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


# Single source of truth for which M&S product-name keywords map to a known
# Ozon category/type -- category_priority.py's is_supported_category() must
# use the SAME keyword groups (via KNOWN_PRODUCT_TYPE_KEYWORDS below), not
# its own separately-maintained list. Confirmed live, 2026-09-04: an
# earlier version of category_priority.py hardcoded its own, much shorter
# keyword list ("kulot", "külot", "tanga", "atlet" only) that was never
# updated when boxer/trunk/brief/slip/hipster/pijama support was added
# here, silently causing discovery to skip categories this pipeline could
# actually translate and upload -- the drift was only caught when adding
# t-shirt support and finding discovery still reported "0 categories
# checked" for it.
UNDERWEAR_KEYWORDS = ("kulot", "külot", "tanga", "boxer", "trunk", "brief", "slip", "hipster")
TANK_TOP_KEYWORDS = ("atlet",)
PAJAMA_KEYWORDS = ("pijama", "pyjama")
TSHIRT_KEYWORDS = ("t-shirt", "tshirt", "tişört", "tisort")

KNOWN_PRODUCT_TYPE_KEYWORDS = UNDERWEAR_KEYWORDS + TANK_TOP_KEYWORDS + PAJAMA_KEYWORDS + TSHIRT_KEYWORDS


def is_known_product_type(name):
    """True if `name` contains a keyword resolve_category_and_type() can
    actually map to a category/type -- used by category_priority.py to
    decide whether a category is worth crawling at all, so the two stay
    in sync by construction rather than by two lists someone has to
    remember to update together."""
    lower = (name or "").lower()
    return any(kw in lower for kw in KNOWN_PRODUCT_TYPE_KEYWORDS)


def resolve_category_and_type(name, is_set_hint):
    """Determines (description_category_id, type_id) from the M&S product name.
    Keyword-based: 'kulot'/'külot'/'tanga'/'boxer'/'trunk'/'brief'/'slip'/
    'hipster' = underwear (Ozon has no separate men's-underwear type --
    confirmed live, 2026-09-01: men's briefs use the exact same
    category/type as women's, just a different gender attribute value, see
    resolve_gender), 'atlet' = tank top, 'pijama'/'pyjama' = pajama,
    't-shirt'/'tshirt'/'tişört' = t-shirt (added 2026-09-04, business
    instruction to give category discovery more supported categories --
    confirmed live: same required-attribute set and RU size dictionary as
    tank top/pajama, no new mapping complications like bras had).
    Returns (None, None) if the product doesn't match a known category --
    caller should skip rather than guess, since an unmapped category means
    unknown required fields."""
    lower = (name or "").lower()
    if any(kw in lower for kw in UNDERWEAR_KEYWORDS):
        return CATEGORY_ID, (TYPE_ID_PANTIES_SET if is_set_hint else TYPE_ID_PANTIES)
    if any(kw in lower for kw in TANK_TOP_KEYWORDS):
        return CATEGORY_ID_CLOTHING, TYPE_ID_TANK_TOP
    if any(kw in lower for kw in PAJAMA_KEYWORDS):
        return CATEGORY_ID_CLOTHING, TYPE_ID_PAJAMA
    if any(kw in lower for kw in TSHIRT_KEYWORDS):
        return CATEGORY_ID_CLOTHING, TYPE_ID_TSHIRT
    return None, None


def resolve_gender(url):
    """Determines the ATTR_GENDER dictionary value id from the M&S URL's
    own erkek-/kadin- gender prefix (confirmed reliable, 2026-09-01: every
    M&S TR product URL starts with "erkek-" (men's) or "kadin-" (women's)
    directly after the domain). Defaults to GENDER_FEMALE_ID when the URL
    doesn't have a recognizable prefix or is missing -- this pipeline's
    catalog is overwhelmingly women's underwear, so that was the prior
    (undetected) default in practice; only flips to male on an explicit
    "erkek" match rather than guessing either way from ambiguous input.

    Fixes a real bug found live, 2026-09-01: attribute 9163 (gender) was
    hardcoded to GENDER_FEMALE_ID for every single product regardless of
    the source URL, meaning any men's product this pipeline uploaded
    (boxer/trunk/brief) would have been incorrectly tagged as women's."""
    if not url:
        return GENDER_FEMALE_ID
    path = url.split("marksandspencer.com.tr/", 1)[-1].lower()
    if path.startswith("erkek-") or "/erkek-" in path:
        return GENDER_MALE_ID
    return GENDER_FEMALE_ID


def log_mapping_decision(log_path, sku, field, input_value, output_value, warning):
    with open(log_path, "a", encoding="utf-8") as f:
        entry = {
            "sku": sku, "field": field, "input": input_value,
            "output": output_value, "warning": warning,
        }
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
