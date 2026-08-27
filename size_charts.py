"""
M&S Turkey's own size-chart data (sourced from marksandspencer.com.tr/size-chart
and cross-checked against a live product page's embedded "Beden Rehberi" modal,
2026-08-27), used for two related tasks (GitHub issues #5 and #6):

  #5 - render one chart image per category (source has no downloadable chart
       images, only HTML tables -- so we generate the image ourselves from
       this same data rather than download something that doesn't exist).
  #6 - push the matching table into Ozon's "Таблица размеров JSON" attribute
       (13164) per product, once that attribute's exact JSON schema is
       confirmed (see backfill_size_chart.py -- blocked on a real example
       exported from the Ozon Seller portal's visual constructor, since
       Ozon's docs are unreachable and no third party publishes the schema).

CATEGORY_KEYWORDS classifies a product by its M&S URL slug (Turkish category
words are reliably present there, e.g. "kulot", "atlet") into one of the
keys in SIZE_CHARTS. Longest/most-specific keywords should be checked before
shorter ones that might be substrings (none currently collide, but keep this
in mind when adding more).
"""

# Ordered so a product matching multiple keywords picks the more specific
# category first (e.g. a boxer-brief hybrid should still land on men's
# underwear before any looser match).
CATEGORY_KEYWORDS = [
    ("boxer", "erkek_ic_giyim"),
    ("trunk", "erkek_ic_giyim"),
    ("termal", "termal"),
    ("corap", "corap"),
    ("atlet", "atlet"),
    ("body", "kadin_ic_giyim"),
    ("jartiyer", "kadin_ic_giyim"),
    ("tanga", "kadin_ic_giyim"),
    ("brazilian", "kadin_ic_giyim"),
    ("bikini", "kadin_ic_giyim"),
    ("hipster", "kadin_ic_giyim"),
    ("slip", "kadin_ic_giyim"),
    ("kulodu", "kadin_ic_giyim"),
    ("kulot", "kadin_ic_giyim"),
    ("sutyen", "sutyen"),
    ("tayt", "tayt"),
    ("sort", "kadin_ic_giyim"),
    ("pijama", "kadin_ic_giyim"),
    ("gecelik", "kadin_ic_giyim"),
]


def classify_category(url):
    """M&S product URL -> one of SIZE_CHARTS's keys, or None if no keyword
    matched (shouldn't happen given CATEGORY_KEYWORDS's current coverage of
    every category in products.csv, confirmed 2026-08-27, but callers should
    still handle None rather than assume)."""
    if not url:
        return None
    slug = url.lower()
    for keyword, category in CATEGORY_KEYWORDS:
        if keyword in slug:
            return category
    return None


# Each chart: title (Turkish, matches M&S's own labeling) + column headers +
# rows. Measurements in cm as published, verified directly against
# marksandspencer.com.tr/size-chart (2026-08-27) -- not estimated/rounded.
SIZE_CHARTS = {
    "kadin_ic_giyim": {
        # M&S's own "KADIN KÜLOTLAR" table (verified against a live product
        # page's embedded "Beden Rehberi" modal, 2026-08-27) -- used for all
        # women's underwear-family products (kulot, tanga, slip, hipster,
        # brazilian, bikini, body, jartiyer, sort, pijama, gecelik).
        "title": "KADIN KÜLOTLAR BEDEN TABLOSU",
        "columns": ["Beden", "İngiltere", "Avrupa", "Bel (cm)", "Kalça (cm)"],
        "rows": [
            ["XS", "6", "34", "61", "86"],
            ["S", "8", "36", "65", "90"],
            ["S", "10", "38", "70", "95"],
            ["M", "12", "40", "75", "100"],
            ["M", "14", "42", "80", "105"],
            ["L", "16", "44", "86", "110"],
            ["L", "18", "46", "92", "115"],
            ["XL", "20", "48", "98", "121"],
            ["XL", "22", "50", "104", "127"],
            ["XXL", "24", "52", "110", "133"],
        ],
    },
    "atlet": {
        # M&S's "Women's Tops" table -- used for atlet (tank top/undershirt).
        "title": "KADIN ÜST GİYİM BEDEN TABLOSU",
        "columns": ["Beden", "İngiltere", "Avrupa", "Göğüs (cm)", "Bel (cm)"],
        "rows": [
            ["XS", "6", "34", "78", "61"],
            ["S", "8-10", "36-38", "82-87", "65-70"],
            ["M", "12-14", "40-42", "92-97", "75-80.5"],
            ["L", "16-18", "44-46", "102.5-108", "86-92"],
            ["XL", "20-22", "48-50", "114-120", "98-104"],
            ["XXL", "24", "52", "126", "110"],
        ],
    },
    "termal": {
        # Thermal wear follows the same body-measurement logic as tops.
        "title": "TERMAL GİYİM BEDEN TABLOSU",
        "columns": ["Beden", "İngiltere", "Avrupa", "Göğüs (cm)", "Bel (cm)"],
        "rows": [
            ["XS", "6", "34", "78", "61"],
            ["S", "8-10", "36-38", "82-87", "65-70"],
            ["M", "12-14", "40-42", "92-97", "75-80.5"],
            ["L", "16-18", "44-46", "102.5-108", "86-92"],
            ["XL", "20-22", "48-50", "114-120", "98-104"],
            ["XXL", "24", "52", "126", "110"],
        ],
    },
    "corap": {
        # M&S's own size-chart page has no dedicated measurement table for
        # plain socks (only for pantyhose/tights) -- reusing the tights
        # table here is the closest real M&S data available, since socks in
        # this catalog are the pantyhose/külotlu çorap style.
        "title": "KÜLOTLU ÇORAP BEDEN ÖLÇÜM TABLOSU",
        "columns": ["Beden", "Boy (cm)", "Etek Bedeni", "Basen (cm)"],
        "rows": [
            ["S", "150-164", "8-12", "86-100"],
            ["M", "150-164", "14-16", "101-107"],
            ["M", "165-174", "8-12", "86-100"],
            ["L", "165-174", "14-16", "101-107"],
            ["L", "177-183", "10-14", "86-100"],
            ["XL", "155-176", "18-24", "108-122"],
        ],
    },
    "sutyen": {
        # M&S's own cup/band conversion grid -- band size (cm) x cup letter
        # maps to a garment size (S/M/L/XL/XXL), not a direct measurement.
        "title": "SUTYEN KUP BEDEN TABLOSU",
        "columns": ["Bant (cm)", "A", "B", "C", "D", "DD", "E", "F", "G", "GG"],
        "rows": [
            ["65/30", "S", "S", "S", "S", "M", "M", "L", "L", "XL"],
            ["70/32", "S", "S", "S", "M", "M", "L", "L", "XL", "XL"],
            ["75/34", "S", "S", "M", "M", "L", "L", "XL", "XL", "XL"],
            ["80/36", "S", "M", "M", "L", "L", "XL", "XL", "XL", "XXL"],
            ["85/38", "M", "M", "L", "L", "XL", "XL", "XL", "XXL", "XXL"],
            ["90/40", "M", "L", "L", "XL", "XL", "XL", "XXL", "XXL", "XXL"],
            ["95/42", "L", "L", "XL", "XL", "XL", "XXL", "XXL", "XXL", "XXL"],
        ],
    },
    "tayt": {
        # Same pantyhose/tights table as "corap" -- M&S doesn't publish a
        # separate leggings-specific chart, and this catalog's "tayt"
        # products are the tights/pantyhose style.
        "title": "KÜLOTLU ÇORAP BEDEN ÖLÇÜM TABLOSU",
        "columns": ["Beden", "Boy (cm)", "Etek Bedeni", "Basen (cm)"],
        "rows": [
            ["S", "150-164", "8-12", "86-100"],
            ["M", "150-164", "14-16", "101-107"],
            ["M", "165-174", "8-12", "86-100"],
            ["L", "165-174", "14-16", "101-107"],
            ["L", "177-183", "10-14", "86-100"],
            ["XL", "155-176", "18-24", "108-122"],
        ],
    },
    "erkek_ic_giyim": {
        # M&S's own "Men's Underwear" table.
        "title": "ERKEK İÇ GİYİM BEDEN TABLOSU",
        "columns": ["Beden", "Bel (cm)"],
        "rows": [
            ["XS", "71-75"],
            ["S", "76-81"],
            ["M", "84-89"],
            ["L", "91-97"],
            ["XL", "99-104"],
            ["XXL", "107-112"],
            ["XXXL", "114-119"],
            ["XXXXL", "120-125"],
        ],
    },
}
