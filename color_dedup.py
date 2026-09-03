"""
Turkish-color-token duplicate guard (business report, 2026-09-03):
"products created before has duplicates because of the colour name in
turkish and english. we need to stop creating new products that has
duplicates."

Root cause (confirmed live, 2026-09-03): legacy_product_urls.csv uses
Turkish color words in its offer_id convention (e.g. MS-T14002558F-MAVI-M),
while this pipeline's own crawler/upload path uses whatever specific
English color name M&S's own page exposes (e.g. BLUEMIX). Both use the
"MS-" prefix, so upload_to_ozon.py's existing duplicate check (which only
compares offer_id PREFIXES, e.g. MS- vs MAR-) never catches this — Ozon's
own duplicate detector (matching on the underlying product, SPU) does, and
either silently creates a visible duplicate listing or rejects the new one
outright with SPU_ALREADY_EXISTS_IN_ANOTHER_ACCOUNT.

A direct Turkish->English WORD translation table was tried and rejected:
checked live against real M&S pages, 2026-09-03: "MAVI" (blue) alone
resolves to four different specific English colors (TEAL, BLUEBELL,
CORNFLOWER, AQUA) across four sample products -- M&S's real per-product
color names are far more specific than the generic Turkish word used in
the legacy offer_id convention, so a 1:1 word table would be systematically
wrong.

Business decision (2026-09-03): "not creating duplicate is much more
important" than allowing every legitimately-different color through --
bias toward SKIPPING (treating as a possible duplicate, don't upload) any
time the match is uncertain. Only treat a Turkish/English color pair as
CLEARLY different products (safe to upload both) when they're in
unambiguous, unrelated color families (e.g. white vs. black) -- see the
business's own example: "unless it is very clear that it is a different
product like white vs SIYAH we can skip it".

This module classifies each color string (Turkish token from a legacy
offer_id, or English string from a fresh crawl) into a broad FAMILY. Two
colors are treated as "possibly the same product" (skip the new upload)
whenever they share a family OR either one's family is unknown/ambiguous.
Two colors are only treated as "clearly different" (safe to upload both)
when they resolve to two different, unambiguous families.
"""
import re

# Turkish color token (as used in legacy_product_urls.csv offer_ids) ->
# SET of broad color families it could plausibly correspond to on any
# given product. Usage counts confirmed live, 2026-09-03 (highest first):
# SIYAH 646, MAVI 399, BEYAZ 340, PEMBE 309, BEJ 205, KREM 135, YESIL 128,
# GRI 124, KIRMIZI 116, KAHVE 164, LACIVERT 144, TURUNCU 50, SARI 44,
# RENKSIZ 31, MOR 30, ACIK 70, BORDO 20, TURK 10, TURQ 10, YENI 10,
# LEOPAR 10, KOYUYESIL 10, KAKAO 8, KUVARS 8, MAVIMIX 5, TAS 5.
#
# Business instruction (2026-09-03): "add all colours and possible
# alternatives to this table, you can use turkish colours in more than one
# row if needed" -- so this is deliberately a many-to-many mapping (each
# Turkish token -> every family it could plausibly mean), not a 1:1
# translation. Confirmed live that a single Turkish word can span multiple
# real families (e.g. MAVI/"blue" resolved to TEAL, BLUEBELL, CORNFLOWER,
# and AQUA across just 4 sample products -- all "blue" family, but also
# genuinely bordering "green" for teal-leaning shades and "purple" for
# some cornflower/lavender-adjacent shades M&S sells).
#
# A token maps to a SINGLE-item set only when it is truly unambiguous
# (its literal meaning admits no other family at all, e.g. BEYAZ/white,
# SIYAH/black are polar opposites in every language/context). A token
# mapping to an EMPTY set is fully ambiguous -- treated as "could be
# anything," per the business's bias toward skipping over risking a
# duplicate.
TURKISH_COLOR_FAMILY = {
    "BEYAZ": {"white"},                                   # white -- unambiguous
    "SIYAH": {"black"},                                    # black -- unambiguous
    "KIRMIZI": {"red", "orange", "burgundy"},              # red -- but M&S red-family shades bleed into coral/orange and wine/burgundy
    "YESIL": {"green"},                                    # green -- unambiguous (confirmed live as "GREEN MIX")
    "KOYUYESIL": {"green"},                                # "dark green" -- unambiguous
    "SARI": {"yellow", "beige"},                           # yellow -- but pale/straw yellows shade into beige (confirmed live as "PALE YELLOW")
    "TURUNCU": {"orange", "red"},                          # orange -- coral/red-orange shades overlap red family
    "MOR": {"purple", "pink"},                             # purple -- lilac/lavender/plum shades border pink family
    "BORDO": {"burgundy", "red", "purple"},                # burgundy/wine -- confirmed live as "CLARET"; dark reds and plums overlap
    "GRI": {"grey", "beige"},                              # grey -- unambiguous enough (confirmed live "GREY MIX"), but "grey marl"/stone-grey borders beige
    "KAHVE": {"brown", "beige"},                           # brown/coffee -- confirmed live "BROWN MIX"; light coffee/tan shades border beige
    "KAKAO": {"brown"},                                    # cocoa -- confirmed live as "FUDGE", brown family
    "BEJ": {"beige", "pink", "brown"},                     # beige -- confirmed live as "ROSE QUARTZ" (pink!); genuinely spans beige/pink/tan
    "KREM": {"beige", "white", "yellow"},                  # cream -- confirmed live "LIGHT STONE"; creams border white and pale yellow
    "TAS": {"beige", "grey"},                              # "stone" -- confirmed live as "STONE"; stone tones border beige/grey
    "MAVI": {"blue", "green", "purple"},                   # blue -- confirmed live: TEAL, BLUEBELL, CORNFLOWER, AQUA (spans blue/green/purple-leaning shades)
    "MAVIMIX": {"blue", "green", "purple"},                # same root as MAVI -- same spread
    "LACIVERT": {"blue"},                                  # navy -- confirmed live as "NAVY MIX", stays within blue family even if shade varies
    "PEMBE": {"pink", "purple", "orange"},                 # pink -- many named shades (dusky/hot/soft/sugar/petal/coral-adjacent pinks)
    "RENKSIZ": set(),                                      # literally "colorless" -- confirmed live as "NEUTRAL", fully ambiguous, could be almost any family
    "LEOPAR": set(),                                       # "leopard" -- confirmed live as "MULTI BROWN", a PATTERN name not a hue; treat as fully ambiguous
    "TURK": set(),                                         # only ever seen appended to another color word (e.g. "MAVI-TURK") -- likely a variant qualifier, not standalone
    "TURQ": {"blue", "green"},                             # likely short for "turquoise" -- turquoise genuinely spans blue/green
    "YENI": set(),                                         # means "new" in Turkish -- likely a variant/restock qualifier, not a color at all
    "KUVARS": {"pink", "beige"},                           # "quartz" -- rose quartz (pink) and other quartz tones (beige/neutral)
    "ACIK": set(),                                         # means "light" in Turkish -- a modifier prefix (e.g. "ACIK PEMBE" = light pink), not a standalone color; always appears compounded
}

# English color family keywords -- used to classify a fresh crawl's own
# color string (already in English) into the same family space as the
# Turkish table above, so the two can be compared. Order matters: more
# specific/compound keywords are checked before generic ones. Plain
# "MULTI" (no other color word alongside it, e.g. the literal value
# "MULTI") is handled separately as fully ambiguous -- but "BLUE MIX",
# "GREEN MIX" etc. are extremely common real values (confirmed in
# products.csv) and clearly ARE a real hue family plus a texture
# qualifier, so MIX/MULTI as a SUFFIX does not blank out the base color.
ENGLISH_COLOR_FAMILY_KEYWORDS = [
    ("white", ("WHITE", "IVORY", "ECRU")),
    ("black", ("BLACK", "NEARLY BLACK", "CHARCOAL")),
    ("red", ("RED", "CLARET", "LACQUER RED", "TOMATO", "GERANIUM")),
    ("burgundy", ("WINE", "CLARET", "BURGUNDY", "RAISIN")),
    ("green", ("GREEN", "SAGE", "EVERGREEN", "MINT", "OLIVE")),
    ("yellow", ("YELLOW",)),
    ("orange", ("ORANGE", "CORAL", "TAWNY")),
    ("purple", ("PURPLE", "LILAC", "LAVENDER", "PLUM")),
    ("grey", ("GREY", "GRAPHITE", "MOLE", "SLATE")),
    ("brown", ("BROWN", "FUDGE", "CHOCOLATE", "MAHOGANY", "COCOA", "CEDAR", "TAWNY")),
    ("pink", ("PINK", "BLUSH", "ROSE", "CYCLAMEN")),
    ("blue", ("BLUE", "NAVY", "TEAL", "AQUA", "TURQUOISE", "DENIM", "CORNFLOWER",
              "BLUEBELL", "CHAMBRAY", "TWILIGHT", "MIDNIGHT", "NIGHTSHADE", "AIR FORCE",
              "AZURE", "WEDGEWOOD", "INK")),
    ("beige", ("BEIGE", "STONE", "OATMEAL", "NATURAL", "BUFF", "CAMEL", "FAWN", "SOFT BROWN",
               "LIGHT CREAM", "PRALINE")),
    ("green", ("KHAKI", "APPLE")),  # khaki and apple-green shades
    ("pink", ("OPAL", "OPALINE", "QUARTZ")),  # M&S's "opal/opaline" and "quartz" shades run pink-adjacent in this catalog (confirmed: ROSE QUARTZ classified pink above)
    ("yellow", ("PALE STRAW", "STRAW")),
]

# Values that carry NO identifiable hue at all -- fully ambiguous,
# distinct from "BLUE MIX" etc. which does have an identifiable base hue.
# JASPER, NEUTRAL, NUDE MIX deliberately left OFF the keyword list above
# (not added here as "confirmed ambiguous" either) -- their real M&S shade
# wasn't checked live, so they simply fall through to an empty/unknown
# classify_english_color() result, which is_color_duplicate_risk() already
# treats as a skip-worthy risk. No need to enumerate them explicitly.
FULLY_AMBIGUOUS_ENGLISH_VALUES = {"MULTI", "MULTI BROWN"}  # MULTI BROWN is a pattern name (leopard-print family), not a plain brown


def classify_turkish_token(token):
    """Returns the SET of possible color families for a legacy Turkish
    offer_id color token. Empty set means unknown/fully ambiguous (never
    seen, or explicitly marked ambiguous in TURKISH_COLOR_FAMILY above)."""
    return TURKISH_COLOR_FAMILY.get(token.upper(), set())



# Suffixes this pipeline's own build_sku() appends directly onto a color
# word with NO separator (e.g. "BLUEMIX" = "BLUE" + "MIX", confirmed live
# in upload_to_ozon.py's color_token = color.replace(" ", "")) -- stripped
# before word-boundary matching so "BLUE" is still found inside
# "BLUEMIX", without reopening the "PINK" vs "INK" substring collision
# (word-boundary matching alone would reject "BLUE" followed directly by
# "M" in "BLUEMIX" just as correctly as it rejects "P" followed by "INK").
NO_SEPARATOR_SUFFIXES = ("MIX",)


def classify_english_color(color_str):
    """Returns the SET of color families a fresh crawl's English color
    string could match -- usually a single-item set, but a compound like
    'PINK/WHITE' legitimately matches two families at once. Empty set
    means no identifiable hue at all (e.g. plain 'MULTI').

    Matches on WORD boundaries, not substring containment -- a naive
    substring check on "INK" (meant to catch "DARK INK") also matches
    inside "PINK", wrongly classifying "PINK MIX" as blue-family too
    (confirmed live, 2026-09-03). Known no-separator suffixes (e.g. the
    "MIX" in offer_id color tokens like "BLUEMIX", which build_sku()
    produces by stripping spaces from "BLUE MIX") are stripped first so
    the base color word still resolves via the same word-boundary check."""
    if not color_str:
        return set()
    upper = color_str.upper()
    if upper in FULLY_AMBIGUOUS_ENGLISH_VALUES:
        return set()
    for suffix in NO_SEPARATOR_SUFFIXES:
        if upper.endswith(suffix) and len(upper) > len(suffix):
            upper = upper[: -len(suffix)]
            break
    families = set()
    for family, keywords in ENGLISH_COLOR_FAMILY_KEYWORDS:
        for kw in keywords:
            if re.search(rf"(?<![A-Z]){re.escape(kw)}(?![A-Z])", upper):
                families.add(family)
                break
    return families


def is_clearly_different_color(families_a, families_b):
    """True only when BOTH sides have at least one identifiable family
    (non-empty sets) AND the two sets share NO family in common. Business
    decision, 2026-09-03: bias toward treating anything uncertain as a
    POSSIBLE duplicate (skip) -- only allow both to be uploaded as
    genuinely different products when there is zero overlap between every
    plausible family each color could be. A single shared family (even if
    each side also has other non-overlapping possibilities) is enough to
    call it a possible match, per "not creating duplicate is much more
    important" (business, 2026-09-03)."""
    if not families_a or not families_b:
        return False
    return families_a.isdisjoint(families_b)


_SIZE_TOKEN_RE = re.compile(
    r"^(XXS|XS|S|M|L|XL|XXL|3XL|4XL|5XL|\d{1,3}(EU)?)$"
)
_KNOWN_PREFIXES = {"MS", "MAR", "SML", "MARKS", "MARK", "SMLMS"}
_ARTICLE_CODE_RE = re.compile(r"^T\d{5,9}[A-Z]{0,2}$")


def extract_color_segment_from_offer_id(offer_id):
    """Returns the color portion of an offer_id (everything between the
    prefix(es)+article code and the trailing size token), joined back
    with hyphens for compounds like 'ACIK-PEMBE'. Returns '' if the shape
    doesn't match PREFIX[-PREFIX]-ARTICLE-COLOR[-COLOR...]-SIZE closely
    enough to isolate a color segment (e.g. legacy numeric-SKU offer_ids).

    Handles compound prefixes like "SML-MS-" (two known-prefix segments in
    a row, confirmed live on real offer_ids) by skipping every leading
    segment that's a known prefix, not just the first one."""
    parts = offer_id.upper().split("-")
    if len(parts) < 3:
        return ""

    start = 0
    while start < len(parts) and parts[start] in _KNOWN_PREFIXES:
        start += 1
    if start < len(parts) and (_ARTICLE_CODE_RE.match(parts[start]) or parts[start].isdigit()):
        start += 1  # also skip the article-code segment

    end = len(parts)
    if _SIZE_TOKEN_RE.match(parts[-1]):
        end = len(parts) - 1

    if start >= end:
        return ""
    return "-".join(parts[start:end])


def extract_turkish_color_tokens_from_offer_id(offer_id):
    """Returns the set of Turkish color tokens (from TURKISH_COLOR_FAMILY)
    that appear in this offer_id's color segment, if any -- an offer_id
    can carry a compound like '-MAVI-TURK-' or '-ACIK-PEMBE-', so this
    returns every matching token found, not just one."""
    segment = extract_color_segment_from_offer_id(offer_id)
    return {p for p in segment.split("-") if p in TURKISH_COLOR_FAMILY}


def offer_id_color_families(offer_id):
    """Union of every family this offer_id's color segment could imply --
    checks BOTH the Turkish-token table (for legacy offer_ids like
    "-MAVI-") AND the English keyword list (for this pipeline's own
    offer_ids like "-BLUEMIX-"), since an offer_id may use either
    convention. Empty set only if NEITHER recognizes anything in the
    offer_id's color segment at all.

    Fixed live, 2026-09-03: an earlier version ran classify_english_color
    on the WHOLE offer_id string rather than just the isolated color
    segment, so "MS-T14002558F-BLUEMIX-40"'s trailing "-40" defeated the
    MIX-suffix-stripping logic (which only strips from the very end of
    whatever string it's given) and the color came back unclassified --
    which is_color_duplicate_risk() then treated as "can't identify,
    assume risk" for every OTHER color checked against that article too,
    overly conservative to the point of blocking legitimate uploads (e.g.
    flagging BLACK as a risk just because one of the article's existing
    listings' color couldn't be classified)."""
    segment = extract_color_segment_from_offer_id(offer_id)
    if not segment:
        return set()
    families = set()
    for token in extract_turkish_color_tokens_from_offer_id(offer_id):
        families |= classify_turkish_token(token)
    families |= classify_english_color(segment)
    return families
