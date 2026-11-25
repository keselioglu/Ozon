"""
Reusable content/media building blocks (task #41), pulled out of the one-off
scripts so a daily job (improve_content.py) can build and push content per
(article, color) group without duplicating their logic:

    generate_extra_photos.py      -> generate_crops()
    generate_product_videos.py    -> generate_video()
    generate_rich_content.py      -> build_rich_content()
    generate_size_chart_table.py  -> size_table_for()
    r2_storage.py                 -> upload_to_r2() / delete_r2()
    push_*_to_ozon.py             -> find_live_ms_offer_ids(), fetch_attributes(),
                                     fetch_prices(), build_merged_import_item(),
                                     push_one_and_verify()

The one-off CLIs keep working as before; they now import the shared Ozon
helpers from here instead of each carrying its own copy.

Safeguards kept from the scripts they came from:
  * Media is keyed by (article, color), never article alone, and offer_ids
    are REBUILT via upload_to_ozon.build_sku() rather than parsed back out of
    an offer_id string. A white product's video once ended up on a black
    offer_id (2026-08-28) because of article-only matching.
  * Only MS-prefix offer_ids are in scope. Legacy prefixes (MAR-/SML-/MARK-/
    ...) have a color token that doesn't reliably match products.csv, so
    they are rejected, not guessed.
  * Ozon's /v3/product/import REPLACES the images list (confirmed live
    2026-09-03), so a push always sends the full combined list, with the real
    M&S photos first and the generated crops after them.
  * A merged import item copies the live record and swaps only the given
    attributes/images. Price/currency/vat are always included: without them
    Ozon sets an invalid negative price and rejects the item
    (push_hashtag_fixes.py pattern).
  * Pushes go ONE item at a time and are confirmed by re-reading
    /v4/product/info/attributes. Never trust /v1/product/import/info: at
    batch scale it reported "imported" for ~94% of items that never got the
    change (push_videos_to_ozon.py, 2026-08-28).

Heavy or network-only dependencies (PIL, requests, boto3, ffmpeg, the 29k-line
translations module) are imported lazily, so this module imports cleanly
offline and in unit tests.
"""
import json
import os
import re
import shutil
import time

from upload_to_ozon import build_sku

MS_PREFIX = "MS-"

PHOTOS_DIR = "generated_photos"
VIDEOS_DIR = "generated_videos"

ATTR_RICH_CONTENT = 11254
ATTR_SIZE_TABLE = 13164
ATTR_VIDEO_LINK = 21841
ATTR_VIDEO_TITLE = 21837
ATTR_VIDEO_COVER = 21845

# Local generated-media folder -> R2 key prefix, matching the keys the push
# scripts have always used ("extra_photos/<folder>/<file>",
# "videos/<folder>/video.mp4").
R2_PREFIX_BY_LOCAL_DIR = {
    PHOTOS_DIR: "extra_photos",
    VIDEOS_DIR: "videos",
}

# Fields copied as-is from a live /v4/product/info/attributes record into an
# import item. Same set every existing push script sends.
_COPIED_RECORD_FIELDS = (
    "weight", "weight_unit", "depth", "width", "height", "dimension_unit",
)


# ---------------------------------------------------------------------------
# (article, color) -> offer_id
# ---------------------------------------------------------------------------

def color_token(color):
    """Same normalization as upload_to_ozon.build_sku's color token."""
    return (color or "").replace(" ", "").upper()


def folder_key_for(article, color):
    """Local folder / R2 key segment for one (article, color) group."""
    return f"{article}_{color_token(color)}"


def is_ms_offer_id(offer_id):
    return isinstance(offer_id, str) and offer_id.startswith(MS_PREFIX)


_HM_OFFER_RE = re.compile(r"^HM-\d{10}-[0-9A-Z]+$")


def is_hm_offer_id(offer_id):
    """H&M offers in this pipeline's own shape HM-<10-digit article>-<size> (run_new_products /
    the live H&M listings). Legacy HM1- / 1-HM- shapes stay out of scope."""
    return isinstance(offer_id, str) and bool(_HM_OFFER_RE.match(offer_id))


def require_content_offer_id(offer_id):
    """MS- or HM-<article>-<size> offer_id, else ValueError (the content job's push scope)."""
    if is_hm_offer_id(offer_id):
        return offer_id
    return require_ms_offer_id(offer_id)


def require_ms_offer_id(offer_id):
    """Raises ValueError for anything that isn't an MS-prefix offer_id."""
    if not is_ms_offer_id(offer_id):
        raise ValueError(
            f"offer_id {offer_id!r} is not MS-prefixed -- legacy prefixes can't be "
            "reliably matched to a color and are out of scope"
        )
    return offer_id


def rebuild_offer_id(article, color, size_label):
    """Expected offer_id for one size of an (article, color) group, rebuilt
    the same way upload_to_ozon.py builds it. Returns None when the size
    label can't be mapped (skip, don't guess)."""
    from ozon_mapping import map_size_to_eu

    if not article:
        return None
    eu_size, _ = map_size_to_eu(size_label)
    if not eu_size:
        return None
    return require_ms_offer_id(build_sku(article, color, eu_size))


def offer_ids_for_group(article, color, size_labels, live_offer_ids=None):
    """Rebuilt MS- offer_ids for every size of one (article, color) group,
    de-duplicated and in input order. If live_offer_ids is given, only
    offer_ids present in it are returned. Legacy-prefix offer_ids in
    live_offer_ids are never matched: rebuilt ids are always MS-, and the
    live set is filtered to MS- as well."""
    live = None
    if live_offer_ids is not None:
        live = {oid for oid in live_offer_ids if is_ms_offer_id(oid)}
    result = []
    for label in size_labels:
        oid = rebuild_offer_id(article, color, label)
        if not oid or oid in result:
            continue
        if live is not None and oid not in live:
            continue
        result.append(oid)
    return result


def split_image_urls(value):
    """products.csv's "|"-separated image_urls -> list of clean URLs."""
    if value is None:
        return []
    try:
        if value != value:  # NaN
            return []
    except Exception:
        pass
    return [u.strip() for u in str(value).split("|") if u.strip()]


# ---------------------------------------------------------------------------
# Generators (local files)
# ---------------------------------------------------------------------------

def generate_crops(article, color, image_urls, target=None, output_dir=PHOTOS_DIR):
    """Zoom crops of the group's real photos so the total reaches `target`
    (default generate_extra_photos.TARGET_PHOTO_COUNT, i.e. 8). One crop per
    distinct real photo at most, so the result may be shorter than needed.
    Returns the list of local file paths."""
    import generate_extra_photos as gep

    if target is None:
        target = gep.TARGET_PHOTO_COUNT
    return gep.generate_extra_photos_for_product(
        folder_key_for(article, color), list(image_urls), output_dir, target=target,
    )


def generate_video(article, color, image_urls, output_dir=VIDEOS_DIR):
    """Ken Burns slideshow MP4 from the group's real photos (needs ffmpeg on
    PATH). Returns the mp4 path, or None if nothing could be rendered."""
    import generate_product_videos as gpv

    video_path, _cover = gpv.build_video_for_product(
        folder_key_for(article, color), list(image_urls), output_dir,
    )
    return video_path


def build_rich_content(article, translation, image_urls):
    """Attribute 11254 rich-content JSON string for one (article, color)
    group. Uses the group's own first real photo, so another color's photo is
    never shown. `article` is only used for the error message."""
    import generate_rich_content as grc

    if not translation:
        raise ValueError(f"no translation for {article!r} -- can't build rich content")
    urls = list(image_urls or [])
    payload = grc.build_rich_content(translation, urls[0] if urls else None)
    return json.dumps(payload, ensure_ascii=False)


_TURKISH_ASCII = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o", "ş": "s", "ü": "u",
    "Ç": "c", "Ğ": "g", "Ö": "o", "Ş": "s", "Ü": "u", "â": "a", "î": "i", "û": "u",
})


def _slugify(text):
    return (text or "").translate(_TURKISH_ASCII).lower()


def size_table_for(name_or_category, url=None):
    """Attribute 13164 size-table JSON string, or None when no size chart
    applies (skip, don't guess). Accepts a size_charts.SIZE_CHARTS key, a
    product name, or an M&S URL. If `url` is given it is tried first, since
    the URL slug is what size_charts.classify_category was built for."""
    from size_charts import SIZE_CHARTS, classify_category
    import generate_size_chart_table as gst

    category_key = None
    if name_or_category in SIZE_CHARTS:
        category_key = name_or_category
    if not category_key and url:
        category_key = classify_category(url)
    if not category_key and name_or_category:
        category_key = classify_category(_slugify(name_or_category))
    if not category_key or category_key not in SIZE_CHARTS:
        return None
    return json.dumps(gst.build_category_size_table(category_key), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Hosting + cleanup
# ---------------------------------------------------------------------------

def r2_object_key_for(path):
    """R2 key for a generated local file: "<prefix>/<folder>/<file>", with
    prefix "extra_photos" for generated_photos/, "videos" for
    generated_videos/, and "content" otherwise."""
    norm = os.path.normpath(path)
    parts = norm.split(os.sep)
    filename = parts[-1]
    folder = parts[-2] if len(parts) >= 2 else ""
    top = parts[-3] if len(parts) >= 3 else ""
    prefix = R2_PREFIX_BY_LOCAL_DIR.get(top, "content")
    return f"{prefix}/{folder}/{filename}" if folder else f"{prefix}/{filename}"


def upload_to_r2(path, object_key=None):
    """Uploads one local file to R2 and returns its public URL."""
    from r2_storage import upload_file

    return upload_file(path, object_key or r2_object_key_for(path))


def delete_r2(keys):
    """Deletes R2 objects by key. Failures are printed, never raised: a
    leftover object is harmless, a crash mid-cleanup is not."""
    from r2_storage import delete_objects  # R2 or Yandex, whichever .env configures

    delete_objects(keys)


# Old name, still used by the push scripts.
delete_r2_objects = delete_r2


def delete_local_dir(path):
    """Removes a generated-media folder (no error if it's already gone)."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def cleanup_group_media(article, color, base_dir, r2_prefix):
    """Deletes one (article, color) group's generated files, both the R2
    copies and the local folder. Call only after every offer_id in the group
    was verified live."""
    folder_key = folder_key_for(article, color)
    product_dir = os.path.join(base_dir, folder_key)
    try:
        files = [f for f in os.listdir(product_dir) if os.path.isfile(os.path.join(product_dir, f))]
    except FileNotFoundError:
        files = []
    delete_r2([f"{r2_prefix}/{folder_key}/{f}" for f in files])
    delete_local_dir(product_dir)


# ---------------------------------------------------------------------------
# Ozon reads
# ---------------------------------------------------------------------------

def _api(api_call):
    if api_call is not None:
        return api_call
    from ozon_client import call
    return call


def find_live_ms_offer_ids(api_call=None):
    """Every live (non-archived) MS-prefix offer_id -- the only prefix whose
    offer_id can be reliably rebuilt from (article, color, eu_size)."""
    call = _api(api_call)
    matches = []
    cursor = ""
    while True:
        params = {"filter": {}, "limit": 1000}
        if cursor:
            params["last_id"] = cursor
        result = call("/v3/product/list", params)
        page = result.get("result", {})
        items = page.get("items", [])
        for item in items:
            oid = item.get("offer_id", "")
            if not item.get("archived") and is_ms_offer_id(oid):
                matches.append(oid)
        cursor = page.get("last_id")
        if not cursor or not items:
            break
    return matches


def fetch_attributes(offer_ids, api_call=None):
    """offer_id -> live /v4/product/info/attributes record."""
    call = _api(api_call)
    offer_ids = list(offer_ids)
    records = {}
    for i in range(0, len(offer_ids), 1000):
        batch = offer_ids[i:i + 1000]
        cursor = ""
        while True:
            params = {"filter": {"offer_id": batch}, "limit": 1000}
            if cursor:
                params["last_id"] = cursor
            result = call("/v4/product/info/attributes", params)
            for item in result.get("result", []):
                records[item["offer_id"]] = item
            cursor = result.get("last_id")
            if not cursor or not result.get("result"):
                break
    return records


def fetch_prices(offer_ids, api_call=None):
    """offer_id -> {"price", "currency_code", "vat"} (strings), from
    /v5/product/info/prices."""
    call = _api(api_call)
    offer_ids = list(offer_ids)
    prices = {}
    for i in range(0, len(offer_ids), 1000):
        batch = offer_ids[i:i + 1000]
        cursor = ""
        while True:
            params = {"filter": {"offer_id": batch}, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = call("/v5/product/info/prices", params)
            for item in result.get("items", []):
                price_info = item.get("price", {})
                prices[item["offer_id"]] = {
                    "price": str(price_info.get("price", "")),
                    "currency_code": price_info.get("currency_code", "USD"),
                    "vat": str(price_info.get("vat", "0")),
                }
            cursor = result.get("cursor")
            if not cursor or not result.get("items"):
                break
    return prices


# ---------------------------------------------------------------------------
# Merged import item
# ---------------------------------------------------------------------------

def combine_images(real_images, generated_images=None):
    """Real photos first (in their order), generated ones after, duplicates
    dropped. Ozon replaces the whole list on import, so this is always the
    full list to send."""
    combined = []
    for url in list(real_images or []) + list(generated_images or []):
        if url and url not in combined:
            combined.append(url)
    return combined


def _attr_values(value):
    """Normalizes an override value to Ozon's attribute "values" list."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return [{"value": str(value)}]


def build_merged_import_item(live_record, price_info, images=None, attr_overrides=None,
                             generated_images=None):
    """/v3/product/import item that copies `live_record` (from
    /v4/product/info/attributes) and changes only what's given:

      attr_overrides   {attr_id: value}. A str/number becomes
                       [{"value": str(v)}], a dict becomes [dict], a list is
                       used as the values list as-is, and None removes the
                       attribute. Overridden attributes keep their position;
                       new ones are appended. Every other attribute is copied
                       unchanged.
      images           full list of real images to send; defaults to the live
                       record's images.
      generated_images appended after the real images (real photos stay
                       first and primary).

    price/currency_code/vat always come from price_info. Raises ValueError for
    an offer_id outside the content job's scope (MS- / HM-<article>-<size>) or a missing price."""
    offer_id = require_content_offer_id(live_record.get("offer_id"))
    if not price_info or not str(price_info.get("price") or "").strip():
        raise ValueError(f"{offer_id}: no live price -- Ozon would reject an import without it")

    overrides = {int(k): v for k, v in (attr_overrides or {}).items()}
    attributes = []
    seen = set()
    for attr in live_record.get("attributes", []):
        attr_id = attr.get("id")
        if attr_id in overrides:
            seen.add(attr_id)
            if overrides[attr_id] is None:
                continue
            attributes.append({"id": attr_id, "values": _attr_values(overrides[attr_id])})
        else:
            attributes.append(attr)
    for attr_id, value in overrides.items():
        if attr_id not in seen and value is not None:
            attributes.append({"id": attr_id, "values": _attr_values(value)})

    base_images = live_record.get("images", []) if images is None else images
    final_images = combine_images(base_images, generated_images)
    primary = live_record.get("primary_image") or (final_images[0] if final_images else None)

    item = {
        "offer_id": offer_id,
        "name": live_record["name"],
        "description_category_id": live_record["description_category_id"],
        "type_id": live_record["type_id"],
        "attributes": attributes,
        "price": str(price_info["price"]),
        "currency_code": price_info.get("currency_code", "USD"),
        "vat": str(price_info.get("vat", "0")),
        "images": final_images,
        "primary_image": primary,
    }
    for field in _COPIED_RECORD_FIELDS:
        item[field] = live_record.get(field)
    return item


# ---------------------------------------------------------------------------
# Push + verify
# ---------------------------------------------------------------------------

def attr_value(record, attr_id):
    """First value of an attribute on a live record, or None."""
    for attr in record.get("attributes", []):
        if attr.get("id") == attr_id and attr.get("values"):
            return attr["values"][0].get("value")
    return None


def check_attr_present(attr_id):
    """Verification check: the attribute has a non-empty value."""
    return lambda record: bool(attr_value(record, attr_id))


def check_attr_equals(attr_id, expected):
    return lambda record: attr_value(record, attr_id) == expected


def check_images_at_least(count):
    return lambda record: len(record.get("images", [])) >= count


def push_one_and_verify(item, checks, api_call=None, attempts=4, first_wait=3, retry_wait=4):
    """Submits ONE item via /v3/product/import, then re-reads it from
    /v4/product/info/attributes until every check passes or `attempts`
    re-reads are used up. A re-read right after the push can be stale
    (2026-09-03), hence the retries. The import/info status is recorded for
    the log but never decides success.

    `checks` is one callable or a list of callables taking the re-read
    record and returning bool. Returns
    {"offer_id", "task_id", "status", "errors", "verified", "record"}."""
    call = _api(api_call)
    offer_id = require_content_offer_id(item.get("offer_id"))
    if callable(checks):
        checks = [checks]
    checks = list(checks)
    if not checks:
        raise ValueError("push_one_and_verify needs at least one check")

    result = call("/v3/product/import", {"items": [item]})
    task_id = (result.get("result") or {}).get("task_id")
    time.sleep(first_wait)

    status_entry = {}
    if task_id:
        try:
            info = call("/v1/product/import/info", {"task_id": task_id})
            info_items = (info.get("result") or {}).get("items", [])
            status_entry = info_items[0] if info_items else {}
        except Exception as e:
            status_entry = {"status": "import_info_error", "errors": [str(e)]}

    record = None
    verified = False
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(retry_wait)
        check = call("/v4/product/info/attributes", {"filter": {"offer_id": [offer_id]}, "limit": 1})
        found = check.get("result", [])
        record = found[0] if found else None
        if record and all(c(record) for c in checks):
            verified = True
            break

    return {
        "offer_id": offer_id,
        "task_id": task_id,
        "status": status_entry.get("status"),
        "errors": status_entry.get("errors"),
        "verified": verified,
        "record": record,
    }
