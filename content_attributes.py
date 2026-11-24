"""
General-properties attribute filler (task #42).

Ozon's content rating (content_rating.py) lists, per SKU, the attributes that
would raise its score (`improve_attributes` of the important_attributes /
other_attributes groups). Many of these are optional, generic, category-level
properties (season, style, fit, neckline, sleeve length, pattern, clasp, age
group, ...) that the live record leaves empty. This module suggests values for
them, for improve_content.py to push as part of its one merged import.

How it works:
  (a) The attribute list of each (description_category_id, type_id) comes from
      /v1/description-category/attribute and is cached in
      content_attr_schema_cache.json. For dictionary attributes, the real values
      come from /v1/description-category/attribute/values (last_value_id
      pagination, limit <= 200 -- see CLAUDE.md) and are cached in
      content_attr_values_cache.json. Both are read-only Ozon calls.
  (b) Claude is asked once per article, with the Russian translation and the
      M&S name/description/specs, to choose dictionary values ONLY from the
      supplied candidates, and free text only for generic, inferable
      properties. The raw answer is cached per article in
      content_attr_suggestions.jsonl, so a re-run costs nothing and gives the
      same result.
  (c) Every answer (fresh or cached) goes through a post-validator: dictionary
      ids not in the cached list are dropped, protected / managed-elsewhere
      attributes are never returned, forbidden-fact attributes (country of
      origin, certification, warranty, measurements, codes) are never filled,
      "unknown"-like answers are dropped, and free text with a percentage
      (e.g. "95% хлопок"), measurements, links or Latin text is rejected.
      If unsure, the attribute stays unset ("skip rather than guess").

Optionally (when the rating's 'text' group is unmet) the same call also
returns a longer Russian description for the rich-content text block, using
only facts already on file. Attribute 4191 (annotation, <= 500 chars) is never
touched here.

Public API:
  suggest_attributes(article, color, record, improve_attribute_ids, ...)
      -> {attr_id: [ {"dictionary_value_id": id, "value": text} | {"value": text} ]}
      (the values-list shape content_media.build_merged_import_item accepts)
  suggest_content(...) -> {"attributes": {...}, "rich_text": str | None}
  improve_attribute_ids_from_rating(rating_record) -> [attr_id, ...]
  text_group_unmet(rating_record) -> bool

Heavy dependencies (anthropic, ozon_client, the translations module) are
imported lazily so this module imports cleanly offline and in unit tests.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

CATEGORY_ID_CLOTHING = 200000933

SCHEMA_CACHE_FILE = "content_attr_schema_cache.json"
VALUES_CACHE_FILE = "content_attr_values_cache.json"
SUGGESTIONS_FILE = "content_attr_suggestions.jsonl"
PRODUCTS_CSV = "products.csv"

VALUES_PAGE_LIMIT = 200          # Ozon's max for attribute/values (CLAUDE.md)
MAX_DICT_VALUES = 600            # bigger dictionaries are not sent to the model -> skipped
MAX_FREE_TEXT_LEN = 300
RICH_TEXT_MIN_LEN = 300
RICH_TEXT_MAX_LEN = 3000
ANNOTATION_MAX_LEN = 500         # attr 4191 limit, kept as today (not written here)

RATING_ATTR_GROUPS = ("important_attributes", "other_attributes")
TEXT_GROUP_KEY = "text"

# Core mapping attributes, set by upload_to_ozon.py / auto_translate.py. Never
# returned by this module, whatever the rating or the model says.
PROTECTED_ATTR_IDS = frozenset({
    9163,   # gender
    10096,  # color
    31,     # brand
    4295,   # RU size
    8292,   # "Merge on One PDP"
    8229,   # type
    4496,   # material
    4191,   # annotation / description
    23171,  # hashtags
})

# Filled by content_media.py's generators, not by this filler.
MANAGED_ELSEWHERE_ATTR_IDS = frozenset({
    11254,  # rich content JSON
    13164,  # size table JSON
    21841,  # video link
    21837,  # video title
    21845,  # video cover (business decision: stays unset)
})

EXCLUDED_ATTR_IDS = PROTECTED_ATTR_IDS | MANAGED_ELSEWHERE_ATTR_IDS

# Attribute names (Russian, lowercase) that would need a specific fact the
# source never reliably gives. Never filled, dictionary or not.
_FORBIDDEN_NAME_RE = re.compile(
    r"страна|производител|изготовител|сертиф|гарант|срок|декларац|"
    r"\bвес|обхват|\bрост|ширин|высот|длина изделия|длина по|размер|"
    r"артикул|код|штрих|тн вэд|партномер|barcode|ean|"
    r"количество|кол-во|комплектац|упаковк|объем|объём|"
    r"процент|плотност|\(см\)|\(мм\)|\(г\)|\(кг\)",
    re.IGNORECASE,
)

# Composition-like attributes: free text allowed only as material names,
# never with percentages.
_COMPOSITION_NAME_RE = re.compile(r"состав|материал|ткан|наполнител|волокн|composition|fabric", re.IGNORECASE)

PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*%")
_MEASUREMENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:см|мм|м|кг|г|гр|л|мл|cm|mm|kg|g)\b", re.IGNORECASE)
_LINK_RE = re.compile(r"https?://|www\.|\.ru\b|\.com\b", re.IGNORECASE)
_LATIN_RE = re.compile(r"[A-Za-z]")
_FORBIDDEN_TEXT_RE = re.compile(
    r"гарант|сертифи|страна|сделано в|произведено|made in|"
    r"посетите|закажите|купите|звоните|скидк|акци|wildberries|вайлдберриз|яндекс",
    re.IGNORECASE,
)

_UNKNOWN_VALUES = {
    "", "-", "—", "?", "n/a", "na", "none", "null", "unknown", "не знаю",
    "неизвестно", "нет данных", "не указано", "не указан", "не определено",
    "нет информации", "unset", "skip",
}


# ---------------------------------------------------------------------------
# Small JSON-file helpers
# ---------------------------------------------------------------------------

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _api(api_call):
    if api_call is not None:
        return api_call
    from ozon_client import call
    return call


# ---------------------------------------------------------------------------
# (a) Schema / dictionary caches (read-only Ozon calls)
# ---------------------------------------------------------------------------

def _schema_key(category_id, type_id):
    return f"{int(category_id)}:{int(type_id)}"


def _values_key(category_id, type_id, attr_id):
    return f"{int(category_id)}:{int(type_id)}:{int(attr_id)}"


def get_type_schema(type_id, category_id=CATEGORY_ID_CLOTHING, api_call=None,
                    cache_file=SCHEMA_CACHE_FILE):
    """{attr_id: attribute dict} for one type, from cache or
    /v1/description-category/attribute."""
    cache = _load_json(cache_file, {})
    key = _schema_key(category_id, type_id)
    if key not in cache:
        resp = _api(api_call)("/v1/description-category/attribute", {
            "description_category_id": int(category_id),
            "type_id": int(type_id),
            "language": "DEFAULT",
        })
        cache[key] = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "type": a.get("type"),
                "is_collection": bool(a.get("is_collection")),
                "is_required": bool(a.get("is_required")),
                "dictionary_id": a.get("dictionary_id") or 0,
                "group_name": a.get("group_name"),
                "max_value_count": a.get("max_value_count"),
            }
            for a in (resp or {}).get("result") or []
            if a.get("id") is not None
        ]
        _save_json(cache_file, cache)
    return {int(a["id"]): a for a in cache[key]}


def get_dictionary_values(type_id, attr_id, category_id=CATEGORY_ID_CLOTHING, api_call=None,
                          cache_file=VALUES_CACHE_FILE, max_values=MAX_DICT_VALUES):
    """Cached entry {"values": [{"id", "value"}], "too_large": bool} for one
    dictionary attribute. Paginates with last_value_id (limit 200). Stops once
    more than max_values were seen and marks the entry too_large -- such
    dictionaries aren't sent to the model (the attribute is skipped)."""
    cache = _load_json(cache_file, {})
    key = _values_key(category_id, type_id, attr_id)
    if key not in cache:
        call = _api(api_call)
        values = []
        too_large = False
        last_value_id = 0
        while True:
            resp = call("/v1/description-category/attribute/values", {
                "description_category_id": int(category_id),
                "type_id": int(type_id),
                "attribute_id": int(attr_id),
                "language": "DEFAULT",
                "last_value_id": last_value_id,
                "limit": VALUES_PAGE_LIMIT,
            }) or {}
            page = resp.get("result") or []
            for v in page:
                if v.get("id") is not None and v.get("value"):
                    values.append({"id": int(v["id"]), "value": v["value"]})
            if len(values) > max_values:
                too_large = True
                break
            if not resp.get("has_next") or not page:
                break
            last_value_id = int(page[-1]["id"])
        cache[key] = {"values": [] if too_large else values, "too_large": too_large}
        _save_json(cache_file, cache)
    return cache[key]


# ---------------------------------------------------------------------------
# Rating helpers
# ---------------------------------------------------------------------------

def improve_attribute_ids_from_rating(rating_record, groups=RATING_ATTR_GROUPS):
    """Attribute ids Ozon's rating asks for in the given groups, in order,
    without duplicates or excluded ids."""
    out = []
    for g in (rating_record or {}).get("groups") or []:
        if g.get("key") not in groups:
            continue
        for a in g.get("improve_attributes") or []:
            try:
                attr_id = int(a.get("id"))
            except (TypeError, ValueError):
                continue
            if attr_id not in EXCLUDED_ATTR_IDS and attr_id not in out:
                out.append(attr_id)
    return out


def text_group_unmet(rating_record):
    """True if the rating's 'text' group has unmet conditions."""
    for g in (rating_record or {}).get("groups") or []:
        if g.get("key") == TEXT_GROUP_KEY and g.get("unmet_conditions"):
            return True
    return False


def _record_has_value(record, attr_id):
    for attr in (record or {}).get("attributes") or []:
        if attr.get("id") == attr_id:
            for v in attr.get("values") or []:
                if str(v.get("value") or "").strip() or v.get("dictionary_value_id"):
                    return True
    return False


def _record_attr_text(record, attr_id):
    for attr in (record or {}).get("attributes") or []:
        if attr.get("id") == attr_id and attr.get("values"):
            return str(attr["values"][0].get("value") or "")
    return ""


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def is_forbidden_attribute(attr):
    name = f"{attr.get('name') or ''}"
    return bool(_FORBIDDEN_NAME_RE.search(name))


def is_composition_attribute(attr):
    return bool(_COMPOSITION_NAME_RE.search(f"{attr.get('name') or ''}"))


def select_candidates(record, improve_attribute_ids, schema, values_lookup):
    """[{attr, values|None}] for the attributes worth asking about: requested
    by the rating, in the type's schema, not excluded, not forbidden, empty on
    the live record, and (for dictionaries) with a usable cached value list."""
    out = []
    seen = set()
    for raw_id in improve_attribute_ids or []:
        try:
            attr_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if attr_id in seen or attr_id in EXCLUDED_ATTR_IDS:
            continue
        seen.add(attr_id)
        attr = schema.get(attr_id)
        if attr is None or is_forbidden_attribute(attr) or _record_has_value(record, attr_id):
            continue
        if attr.get("dictionary_id"):
            entry = values_lookup(attr_id) or {}
            if entry.get("too_large") or not entry.get("values"):
                continue
            out.append({"attr": attr, "values": entry["values"]})
        else:
            out.append({"attr": attr, "values": None})
    return out


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _is_unknown(text):
    return str(text or "").strip().strip(".").lower() in _UNKNOWN_VALUES


def validate_free_text(attr, text):
    """Cleaned text, or None if it must not be written. Rejects unknown-like
    answers, percentages (always for composition-like attributes, and for any
    other free text too -- no generic property needs one), measurements,
    links, Latin text and facts we never invent."""
    if text is None or not isinstance(text, str):
        return None
    cleaned = " ".join(text.split())
    if _is_unknown(cleaned) or len(cleaned) > MAX_FREE_TEXT_LEN:
        return None
    if PERCENT_RE.search(cleaned):
        return None
    if _MEASUREMENT_RE.search(cleaned) or _LINK_RE.search(cleaned):
        return None
    if _LATIN_RE.search(cleaned) or _FORBIDDEN_TEXT_RE.search(cleaned):
        return None
    if is_forbidden_attribute(attr or {}):
        return None
    return cleaned


def validate_dictionary_ids(attr, ids, allowed_values):
    """[{"dictionary_value_id", "value"}] for ids that are really in the
    cached dictionary. A non-collection attribute with more than one valid id
    is ambiguous -> nothing (skip rather than guess)."""
    by_id = {int(v["id"]): v["value"] for v in allowed_values or []}
    picked = []
    for raw in ids or []:
        try:
            vid = int(raw)
        except (TypeError, ValueError):
            continue
        if vid in by_id and all(p["dictionary_value_id"] != vid for p in picked):
            if _is_unknown(by_id[vid]):
                continue
            picked.append({"dictionary_value_id": vid, "value": by_id[vid]})
    if not picked:
        return None
    if not attr.get("is_collection") and len(picked) > 1:
        return None
    max_count = attr.get("max_value_count")
    if isinstance(max_count, int) and max_count > 0 and len(picked) > max_count:
        return None
    return picked


def validate_rich_text(text, annotation=""):
    """Cleaned long description, or None. Pure prose in Russian: no hashtags,
    links, Latin, percentages, measurements or invented facts."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if _is_unknown(cleaned):
        return None
    if not (RICH_TEXT_MIN_LEN <= len(cleaned) <= RICH_TEXT_MAX_LEN):
        return None
    if "#" in cleaned or _LINK_RE.search(cleaned) or _LATIN_RE.search(cleaned):
        return None
    if PERCENT_RE.search(cleaned) or _MEASUREMENT_RE.search(cleaned):
        return None
    if _FORBIDDEN_TEXT_RE.search(cleaned):
        return None
    if annotation and cleaned == annotation.strip():
        return None
    return cleaned


def validate_answer(answer, candidates):
    """{attr_id: values list} from a raw model answer, keeping only what
    passes validation for an attribute in `candidates`."""
    by_id = {int(c["attr"]["id"]): c for c in candidates}
    out = {}
    for item in (answer or {}).get("attributes") or []:
        if not isinstance(item, dict):
            continue
        try:
            attr_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if attr_id in EXCLUDED_ATTR_IDS or attr_id in out:
            continue
        cand = by_id.get(attr_id)
        if cand is None:
            continue
        attr = cand["attr"]
        if cand["values"] is not None:
            picked = validate_dictionary_ids(attr, item.get("dictionary_value_ids"), cand["values"])
            if picked:
                out[attr_id] = picked
        else:
            text = validate_free_text(attr, item.get("value"))
            if text:
                out[attr_id] = [{"value": text}]
    return out


# ---------------------------------------------------------------------------
# Suggestion cache (content_attr_suggestions.jsonl)
# ---------------------------------------------------------------------------

def load_cached_answers(article, type_id, path=SUGGESTIONS_FILE):
    """Merged cached answer for (article, type_id): {"asked": set,
    "attributes": [...], "rich_asked": bool, "rich_description": str|None}.
    Later lines win for the same attribute id."""
    merged = {"asked": set(), "attributes": {}, "rich_asked": False, "rich_description": None}
    if not os.path.exists(path):
        return None
    found = False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("article") != article or int(row.get("type_id") or 0) != int(type_id):
                continue
            found = True
            merged["asked"].update(int(a) for a in row.get("asked") or [])
            for item in (row.get("answer") or {}).get("attributes") or []:
                try:
                    merged["attributes"][int(item.get("id"))] = item
                except (TypeError, ValueError):
                    continue
            if row.get("rich_asked"):
                merged["rich_asked"] = True
                rich = (row.get("answer") or {}).get("rich_description")
                if rich:
                    merged["rich_description"] = rich
    if not found:
        return None
    merged["attributes"] = list(merged["attributes"].values())
    return merged


def append_cached_answer(article, color, type_id, asked, rich_asked, answer, path=SUGGESTIONS_FILE):
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "article": article,
        "color": color,
        "type_id": int(type_id),
        "asked": sorted(int(a) for a in asked),
        "rich_asked": bool(rich_asked),
        "answer": answer,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# (b) Claude call
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You fill optional Ozon marketplace attributes for Marks & Spencer clothing \
listings (Russian seller account). You are given the product's Russian listing text, the original \
Turkish M&S name/description/specs, and a list of attributes to consider.

Hard rules:
- For a DICTIONARY attribute, answer only with ids from that attribute's "candidates" list. Never \
invent, recall or guess an id that is not in the list. Pick a value only when the source text clearly \
supports it. For a single-value attribute give at most one id.
- For a FREE-TEXT attribute, only give a short generic, category-level property that follows clearly \
from the source (e.g. season, style/occasion, fit, neckline, sleeve length, pattern, clasp type, age \
group, care instructions already on the page). Write it in Russian, no Latin letters.
- Never state fiber percentages, certifications, warranty terms, country of origin, measurements, \
weights or any number that is not in the source. No links, no calls to action, no marketplace names.
- If you are not sure, leave the attribute out of your answer. An omitted attribute is always better \
than a wrong one.
- "rich_description": if requested, write 3-6 sentences of descriptive Russian prose (no hashtags, \
no lists of keywords, no Latin letters) using ONLY facts present in the given texts. Otherwise null.

Respond with ONLY the JSON object."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "dictionary_value_ids": {"type": "array", "items": {"type": "integer"}},
                    "value": {"type": ["string", "null"]},
                },
                "required": ["id", "dictionary_value_ids", "value"],
                "additionalProperties": False,
            },
        },
        "rich_description": {"type": ["string", "null"]},
    },
    "required": ["attributes", "rich_description"],
    "additionalProperties": False,
}


def _model():
    from auto_translate import MODEL
    return MODEL


def build_user_prompt(candidates, record, translation, source, want_rich_text):
    attrs = []
    for c in candidates:
        a = c["attr"]
        entry = {
            "id": a["id"],
            "name": a.get("name"),
            "description": (a.get("description") or "")[:300],
            "kind": "dictionary" if c["values"] is not None else "free_text",
            "multiple_values_allowed": bool(a.get("is_collection")),
        }
        if c["values"] is not None:
            entry["candidates"] = [{"id": v["id"], "value": v["value"]} for v in c["values"]]
        attrs.append(entry)
    product = {
        "ru_name": (record or {}).get("name"),
        "ru_annotation": _record_attr_text(record, 4191),
        "ru_translation": {k: v for k, v in (translation or {}).items()
                           if k in ("name", "description", "material_text",
                                    "material_composition", "care_text")},
        "ms_name_tr": (source or {}).get("name"),
        "ms_description_tr": (source or {}).get("description"),
        "ms_specs": (source or {}).get("specs"),
    }
    return (
        f"Product:\n{json.dumps(product, ensure_ascii=False, indent=1)}\n\n"
        f"Attributes to consider:\n{json.dumps(attrs, ensure_ascii=False, indent=1)}\n\n"
        f"rich_description requested: {'yes' if want_rich_text else 'no (return null)'}\n\n"
        "Respond with only the JSON object."
    )


def ask_model(client, candidates, record, translation, source, want_rich_text):
    """Raw answer dict, or None on any failure (caller skips)."""
    try:
        import anthropic
        api_error = anthropic.APIError
    except ImportError:  # pragma: no cover
        api_error = Exception
    try:
        response = client.messages.create(
            model=_model(),
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": build_user_prompt(
                candidates, record, translation, source, want_rich_text)}],
        )
    except api_error as e:
        print(f"  content_attributes: Anthropic API call failed: {e}")
        return None
    if getattr(response, "stop_reason", None) == "refusal":
        return None
    text_block = next((b for b in response.content or [] if getattr(b, "type", None) == "text"), None)
    if text_block is None:
        return None
    try:
        answer = json.loads(text_block.text)
    except (json.JSONDecodeError, TypeError):
        return None
    return answer if isinstance(answer, dict) else None


# ---------------------------------------------------------------------------
# Source loaders (lazy)
# ---------------------------------------------------------------------------

_translations = None
_sources = None


def load_translation(article):
    global _translations
    if _translations is None:
        try:
            from ozon_translations import PRODUCT_TRANSLATIONS
            _translations = PRODUCT_TRANSLATIONS
        except ImportError:
            _translations = {}
    return _translations.get(article)


def load_source(article, path=PRODUCTS_CSV):
    """{"name", "description", "specs"} for an article from products.csv, or None."""
    global _sources
    if _sources is None:
        _sources = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    code = row.get("ms_article_code")
                    if not code or code in _sources:
                        continue
                    try:
                        specs = json.loads(row.get("specs_json") or "{}")
                    except json.JSONDecodeError:
                        specs = {}
                    _sources[code] = {
                        "name": row.get("name"),
                        "description": row.get("description"),
                        "specs": specs,
                    }
    return _sources.get(article)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def suggest_content(article, color, record, improve_attribute_ids, want_rich_text=False,
                    client=None, api_call=None, translation=None, source=None,
                    schema_cache=SCHEMA_CACHE_FILE, values_cache=VALUES_CACHE_FILE,
                    suggestions_file=SUGGESTIONS_FILE):
    """{"attributes": {attr_id: values}, "rich_text": str|None} for one
    (article, color) group's live record. Reuses the cached answer for the
    article when it already covers what's asked; otherwise calls Claude once
    for the missing part and appends it to the cache. Everything returned is
    validated against the current schema/dictionary caches."""
    empty = {"attributes": {}, "rich_text": None}
    type_id = (record or {}).get("type_id")
    category_id = (record or {}).get("description_category_id") or CATEGORY_ID_CLOTHING
    if not type_id:
        return empty

    schema = get_type_schema(type_id, category_id, api_call=api_call, cache_file=schema_cache)

    def values_lookup(attr_id):
        return get_dictionary_values(type_id, attr_id, category_id, api_call=api_call,
                                     cache_file=values_cache)

    candidates = select_candidates(record, improve_attribute_ids, schema, values_lookup)
    if not candidates and not want_rich_text:
        return empty

    wanted_ids = {int(c["attr"]["id"]) for c in candidates}
    cached = load_cached_answers(article, type_id, suggestions_file)
    missing = [c for c in candidates if cached is None or int(c["attr"]["id"]) not in cached["asked"]]
    need_rich = bool(want_rich_text) and (cached is None or not cached["rich_asked"])

    answer_attrs = list(cached["attributes"]) if cached else []
    rich = cached["rich_description"] if cached else None

    if missing or need_rich:
        if translation is None:
            translation = load_translation(article)
        if source is None:
            source = load_source(article)
        if not translation and not source:
            print(f"  content_attributes: {article}: no translation or M&S source on file -- skipping")
        else:
            if client is None:
                import anthropic
                client = anthropic.Anthropic()
            fresh = ask_model(client, missing, record, translation, source, need_rich)
            if fresh is not None:
                append_cached_answer(article, color, type_id,
                                     [c["attr"]["id"] for c in missing], need_rich, fresh,
                                     suggestions_file)
                fresh_ids = {int(c["attr"]["id"]) for c in missing}
                answer_attrs = [a for a in answer_attrs
                                if _safe_int(a.get("id")) not in fresh_ids]
                answer_attrs += list(fresh.get("attributes") or [])
                if need_rich:
                    rich = fresh.get("rich_description")

    attributes = validate_answer({"attributes": answer_attrs},
                                 [c for c in candidates if int(c["attr"]["id"]) in wanted_ids])
    rich_text = None
    if want_rich_text:
        rich_text = validate_rich_text(rich, _record_attr_text(record, 4191))
    return {"attributes": attributes, "rich_text": rich_text}


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def suggest_attributes(article, color, record, improve_attribute_ids, **kwargs):
    """{attr_id: values list} of validated suggestions for the attributes the
    rating asks for and the live record leaves empty. Empty dict if none."""
    kwargs.pop("want_rich_text", None)
    return suggest_content(article, color, record, improve_attribute_ids,
                           want_rich_text=False, **kwargs)["attributes"]


# ---------------------------------------------------------------------------
# CLI (dry-run preview only -- never writes to Ozon)
# ---------------------------------------------------------------------------

def main(argv=None):
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="Preview general-property attribute suggestions for one offer_id "
                    "(read-only toward Ozon; prints, never pushes).")
    ap.add_argument("offer_id", help="MS- offer_id, e.g. MS-T123-BLACK-40")
    ap.add_argument("--article", required=True, help="M&S article code")
    ap.add_argument("--color", default="")
    ap.add_argument("--rich-text", action="store_true", help="also ask for a longer description")
    args = ap.parse_args(argv)

    import content_media as cm
    from content_rating import SNAPSHOT_FILE

    record = cm.fetch_attributes([args.offer_id]).get(args.offer_id)
    if not record:
        print(f"{args.offer_id}: no live record")
        return 1
    rating = _load_json(SNAPSHOT_FILE, {}).get(args.offer_id) or {}
    ids = improve_attribute_ids_from_rating(rating)
    result = suggest_content(args.article, args.color, record, ids,
                             want_rich_text=args.rich_text or text_group_unmet(rating))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
