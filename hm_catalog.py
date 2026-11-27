"""
H&M new-products catalog: which H&M Turkey sections we take products from, how an H&M
listing hit maps onto Ozon (category/type, RU size, colour), and how an Ozon import item is
built. Read-only toward H&M; pure apart from the Ozon dictionary lookups in ozon_mapping.

Rules (business, 2026-11-25 -- same "skip rather than guess" rule as the M&S pipeline):
  - women's sections only (gender = female);
  - Ozon category/type from the Turkish H&M title via the SAME keyword cascade the M&S upload
    uses (ozon_mapping.resolve_category_and_type); no match -> skipped;
  - size: letter XXS..5XL -> RU via LETTER_TO_RU_SIZE; EU number 32..56 -> RU = EU + 6
    (same chart as M&S: EU 34 = RU 40); socks -> "universal"; anything else -> skipped;
  - colour: exact match in HM_COLORS after stripping pattern words; unknown -> skipped;
  - an article (10 digits = product + colour) that is live on Ozon under any H&M prefix is
    skipped as a whole (no duplicates);
  - stock: H&M listing stock code 2 (in stock) -> 5, 1 (few left) -> 1, 0 -> 0 -- the same
    5/1/0 rule refresh_hm_stock.py uses for live H&M products;
  - price: H&M's current TRY price x upload_to_ozon.TRY_TO_USD_RATE (same rule as M&S uploads).
offer_id: HM-<10-digit article>-<H&M size name>, like the live H&M listings.
"""
import json
import re

from ozon_mapping import (
    ATTR_BRAND, ATTR_COLOR, ATTR_GENDER, ATTR_SIZE, ATTR_TYPE, CATEGORY_ID_CLOTHING, GENDER_FEMALE_ID,
    LETTER_TO_RU_SIZE, RU_SIZE_UNIVERSAL_ID, TYPE_ID_SOCKS, _load_color_dict, _load_size_dict,
    hashtag_attribute, resolve_category_and_type,
)

BRAND_HM_ID = 5097273          # Ozon brand dictionary "H&M" (as on the live H&M listings)
BRAND_PREFIX = "H&M "
MERGE_ATTR_ID = 8292
STOCK_BY_CODE = {2: 5, 1: 1, 0: 0}

# H&M Turkey women's "shop by product" sections that can hold types we can list.
HM_SECTIONS = [
    "/tr_tr/kadin/urune-gore-satin-al/elbise.html",
    "/tr_tr/kadin/urune-gore-satin-al/pantolon.html",
    "/tr_tr/kadin/urune-gore-satin-al/jean.html",
    "/tr_tr/kadin/urune-gore-satin-al/etek.html",
    "/tr_tr/kadin/urune-gore-satin-al/gomlek-bluz.html",
    "/tr_tr/kadin/urune-gore-satin-al/ust.html",
    "/tr_tr/kadin/urune-gore-satin-al/hirka-kazak.html",
    "/tr_tr/kadin/urune-gore-satin-al/ceket-kaban.html",
    "/tr_tr/kadin/urune-gore-satin-al/blazer-yelek.html",
    "/tr_tr/kadin/urune-gore-satin-al/ic-camasir.html",
    "/tr_tr/kadin/urune-gore-satin-al/ev-kiyafetleri.html",
    "/tr_tr/kadin/urune-gore-satin-al/gecelik.html",
    "/tr_tr/kadin/urune-gore-satin-al/corap-kulotlu-corap.html",
]

# Turkish H&M colour phrase (lower-case, after pattern words are stripped) -> Ozon colour value.
HM_COLORS = {
    "siyah": "black", "beyaz": "white", "kırık beyaz": "creamy white", "krem": "cream", "ekru": "ivory",
    "bej": "beige", "açık bej": "light beige", "koyu bej": "dark beige", "gri": "grey", "açık gri": "light grey",
    "koyu gri": "dark grey", "antrasit": "Anthracite", "lacivert": "dark blue", "koyu lacivert": "dark blue",
    "mavi": "blue", "açık mavi": "light blue", "koyu mavi": "dark blue", "kırmızı": "red", "koyu kırmızı": "deep red",
    "bordo": "burgundy", "pembe": "pink", "açık pembe": "light pink", "koyu pembe": "dark pink",
    "pudra": "Pink Powder", "fuşya": "fuchsia", "yeşil": "green", "açık yeşil": "light green",
    "koyu yeşil": "dark green", "haki": "khaki", "koyu haki": "khaki", "açık haki": "Light Khaki",
    "zeytin yeşili": "olive green", "mint yeşili": "mint green", "sarı": "yellow", "açık sarı": "light yellow",
    "hardal sarısı": "mustard", "turuncu": "orange", "mercan": "coral", "mor": "purple", "lila": "lilac",
    "açık lila": "light lilac", "kahverengi": "brown", "açık kahverengi": "light brown",
    "koyu kahverengi": "dark brown", "turkuaz": "turquoise", "gümüş rengi": "silver", "altın rengi": "gold",
    "vizon": "taupe", "camel": "Caramel", "taba": "ginger", "kum rengi": "sandy",
}
# Pattern / texture words: dropped before the colour lookup ("Koyu gri kırçıllı" -> "koyu gri").
PATTERN_WORDS = {"kırçıllı", "melanj", "desenli", "çizgili", "benekli", "puantiyeli", "çiçekli", "yıkamalı",
                 "mat", "parlak", "eskitme", "ekose", "kareli", "baskılı", "leopar", "zebra", "denim"}
_LETTERS = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL"}


def hm_color(color_name):
    """-> Ozon colour value (str) or None. Takes the part before "/" (H&M lists the base colour
    first, e.g. "Koyu kahverengi/Leopar desenli"); compounds like "Gri-bej" -> None."""
    if not color_name:
        return None
    base = color_name.split("/")[0].strip().lower().replace("i̇", "i")
    words = [w for w in base.split() if w not in PATTERN_WORDS]
    return HM_COLORS.get(" ".join(words)) if words else None


def ru_size(size_name, is_socks=False):
    """-> (ozon size dictionary id, ru size str, offer_id token) or (None, None, None)."""
    name = (size_name or "").strip().upper().replace(" ", "")
    sizes = _load_size_dict()
    if is_socks:
        token = re.sub(r"[^0-9A-Z]", "", name)
        return (RU_SIZE_UNIVERSAL_ID, "universal", token) if token else (None, None, None)
    if name in _LETTERS:
        ru = LETTER_TO_RU_SIZE.get(name)
    elif name.isdigit() and 32 <= int(name) <= 56 and int(name) % 2 == 0:
        ru = str(int(name) + 6)
    else:
        return None, None, None
    vid = sizes.get(ru)
    return (vid, ru, name) if vid else (None, None, None)


def classify(title):
    """-> (description_category_id, type_id) via the shared M&S keyword cascade on the Turkish
    title: the garment word that comes LAST wins ("Slip Etek" = skirt, "Tişört Elbise" = dress),
    tights ("külotlu çorap") are skipped -- see ozon_mapping.resolve_category_and_type()."""
    t = (title or "").lower()
    is_set = bool(re.search(r"\d+\s*'?\s*l[iıuü]\b", t)) or "set" in t.split()
    return resolve_category_and_type(title, is_set)


def offer_id(article, token):
    return f"HM-{article}-{token}"


def summarize_hit(hit):
    """Listing hit -> compact record used by the walk / queue (no Ozon calls except dictionaries)."""
    cat, type_id = classify(hit.get("title"))
    color_name = (hit.get("productColor") or {}).get("colorName")
    prices = hit.get("prices") or [{}]
    price = min((p.get("price") for p in prices if isinstance(p.get("price"), (int, float))), default=None)
    sizes = []
    for s in hit.get("sizes") or []:
        sid, ru, token = ru_size(s.get("name"), is_socks=(type_id == TYPE_ID_SOCKS))
        stock = STOCK_BY_CODE.get(s.get("stock"), 0)
        sizes.append({"name": s.get("name"), "token": token, "ru": ru, "size_id": sid, "stock": stock})
    return {"article": hit.get("articleCode"), "title": hit.get("title"), "hm_category": hit.get("category"),
            "color_name": color_name, "ozon_color": hm_color(color_name), "price_try": price,
            "category_id": cat, "type_id": type_id, "image": hit.get("imageProductSrc"), "sizes": sizes}


def sellable_sizes(rec):
    return [s for s in rec["sizes"] if s["size_id"] and s["stock"] > 0]


def skip_reason(rec, live_articles):
    if not rec.get("article") or not re.fullmatch(r"\d{10}", rec["article"]):
        return "no article code"
    if rec["article"] in live_articles:
        return "already on Ozon"
    if not rec["type_id"]:
        return "category not mapped"
    if not rec["ozon_color"]:
        return "colour not mapped"
    if not rec["price_try"]:
        return "no price"
    if not sellable_sizes(rec):
        return "no in-stock mappable size"
    return None


def specs_for_translation(detail):
    """Facts from the H&M product page for auto_translate (description, composition, care)."""
    v = detail["variation"]
    return json.dumps({"Açıklama": v.get("description"), "Materyal": ", ".join(v.get("compositions") or []),
                       "Bakım": "; ".join(v.get("careInstructions") or []), "Renk": v.get("name"),
                       "Marka": "H&M"}, ensure_ascii=False)


def images_for(detail, limit=10):
    out = []
    for im in detail["variation"].get("images") or []:
        u = im.get("baseUrl") or ""
        if u.startswith("https://") and u not in out:
            out.append(u)
    return out[:limit]


def build_items(rec, detail, translation, usd_rate, dims):
    """-> list of Ozon /v3/product/import items, one per sellable size. translation = the
    auto_translate entry (name, description, care_text, material_composition, hashtags, ...)."""
    images = images_for(detail)
    if not images:
        return []
    color_id = _load_color_dict().get(rec["ozon_color"])
    if not color_id:
        return []
    from ozon_translations import COLLECTION_ID, WARRANTY_ID
    from upload_to_ozon import (ATTR_ANNOTATION, ATTR_CARE, ATTR_COLLECTION, ATTR_HASHTAGS, ATTR_MATERIAL,
                                ATTR_MATERIAL_COMPOSITION, ATTR_PLANTING_TYPE, ATTR_WARRANTY)
    usd = round(float(rec["price_try"]) * usd_rate, 2)
    items = []
    for s in sellable_sizes(rec):
        attrs = [
            {"id": ATTR_SIZE, "values": [{"dictionary_value_id": s["size_id"]}]},
            {"id": ATTR_COLOR, "values": [{"dictionary_value_id": color_id}]},
            {"id": ATTR_GENDER, "values": [{"dictionary_value_id": GENDER_FEMALE_ID}]},
            {"id": ATTR_BRAND, "values": [{"dictionary_value_id": BRAND_HM_ID}]},
            {"id": MERGE_ATTR_ID, "values": [{"value": f"HM-{rec['article'][:7]}"}]},
            {"id": ATTR_CARE, "values": [{"value": translation["care_text"]}]},
            {"id": ATTR_ANNOTATION, "values": [{"value": translation["description"][:500]}]},
            {"id": ATTR_MATERIAL_COMPOSITION, "values": [{"value": translation["material_composition"]}]},
            {"id": ATTR_COLLECTION, "values": [{"dictionary_value_id": COLLECTION_ID}]},
            {"id": ATTR_WARRANTY, "values": [{"dictionary_value_id": WARRANTY_ID}]},
        ] + hashtag_attribute(ATTR_HASHTAGS, translation.get("hashtags"))
        if translation.get("material_id"):
            attrs.append({"id": ATTR_MATERIAL, "values": [{"dictionary_value_id": translation["material_id"]}]})
        if translation.get("planting_type_id"):
            attrs.append({"id": ATTR_PLANTING_TYPE, "values": [{"dictionary_value_id": translation["planting_type_id"]}]})
        if rec["category_id"] == CATEGORY_ID_CLOTHING:
            attrs.append({"id": ATTR_TYPE, "values": [{"dictionary_value_id": rec["type_id"]}]})
        items.append({
            "offer_id": offer_id(rec["article"], s["token"]),
            "name": BRAND_PREFIX + translation["name"],
            "description_category_id": rec["category_id"], "type_id": rec["type_id"],
            "price": str(usd), "currency_code": "USD",
            "weight": dims["weight"], "weight_unit": "g", "depth": dims["depth"], "width": dims["width"],
            "height": dims["height"], "dimension_unit": "cm",
            "images": images, "primary_image": images[0], "attributes": attrs, "vat": "0",
            "_stock": s["stock"],
        })
    return items
