"""
Generates a Russian translation entry for a newly-crawled, untranslated product by
calling the Anthropic API directly, and appends it to PRODUCT_TRANSLATIONS in
ozon_translations.py.

This exists for unattended (Task Scheduler) runs where no one is available to write
the translation by hand, as was done for the first 24 products. It targets the exact
same style, structure, and Ozon content-policy constraints as those hand-written
entries — see ozon_translations.py's module docstring and existing entries, which are
fed to the model as few-shot examples below.
"""
import json
import re
import sys

import anthropic

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_mapping import resolve_category_and_type

MODEL = "claude-opus-5"

# Material dictionary IDs already verified against Ozon's live attribute dictionary
# (see ozon_translations.py's existing entries) — reused rather than re-queried per
# product, since the same three materials cover every product so far. If the source
# material text doesn't clearly match one of these, material_id is left null rather
# than guessed, same as the "Synthetic" case in the T81006849L entry.
KNOWN_MATERIAL_IDS = {
    "модал": 61952,
    "хлопок": 62174,
    "вискоза": 61786,
}

# planting_type_id values already verified against Ozon's live dictionary, reused from
# the existing underwear entries. Only relevant for underwear (CATEGORY_ID); never set
# for tank tops or other categories.
PLANTING_TYPE_HIGH = 45007   # "High" — high-leg / high-waist cuts
PLANTING_TYPE_MEDIUM = 45009  # "Medium" — shorts / Brazilian cuts
PLANTING_TYPE_LOW = 45008     # "Low" — thong cuts

SYSTEM_PROMPT = """You are writing Russian-language Ozon marketplace listing content for Marks & Spencer \
women's underwear and loungewear products sold via a Turkish M&S storefront, for a Russian seller account.

You will be given the product's Turkish source name and any specs scraped from the source page. Write a \
Russian translation entry as JSON matching the exact schema and style shown in the examples.

Hard rules (Ozon rejects listings that violate these):
- "name" and "description" must be entirely in Cyrillic — zero Latin characters, including no English \
product-line names left untranslated. Transliterate brand/line names into Cyrillic (e.g. "Rosalie" -> "Розали").
- "description" must be purely descriptive of the product itself. No external links, no seller contact info, \
no calls to action ("посетите наш сайт", "смотрите наш ассортимент"), no comparisons to other brands or \
marketplaces, no mentions of other marketplaces.
- Do not fabricate specific facts you don't have: no invented fiber percentages, no invented certifications, \
no invented warranty terms. If you don't know the exact composition, state only the material name(s) you can \
infer from the source text (e.g. "Состав: хлопок, эластан") without percentages — elastane/spandex trim is a \
safe inference for stretch underwear/loungewear if the source implies stretch, but do not invent it if nothing \
suggests it.
- "material_id" must be null unless the material clearly matches one of these known dictionary entries by name: \
модал (Modal), хлопок (Cotton), вискоза (Viscose). If the source material is something else entirely (e.g. \
synthetic/polyester with no clean match), set material_id to null and still describe it honestly in \
material_text/material_composition.
- "planting_type_id" must be null for anything that is not underwear (tank tops, tops, etc. never get this \
field). For underwear, use 45007 (high leg/waist), 45009 (medium/shorts/Brazilian), or 45008 (low/thong) based \
on the cut described in the source name, or null if the cut isn't specified.
- "care_text" should be the same standard care instructions used across the existing catalog unless the source \
clearly implies different care (e.g. hand-wash-only for lace), matching this house style: \
"Машинная стирка при 30°C. Не отбеливать. Сушить при низкой температуре." (or the hand-wash variant for delicate \
lace pieces, matching the T81006849L example).
- "hashtags" should follow the exact format of the examples: 4-5 lowercase Russian hashtags plus the fixed final \
tag #MarksSpencer, space-separated, each starting with #, no spaces within a tag.
- Match the tone, sentence structure, and length of the examples closely — two short sentences: one describing \
the garment/fit/fabric, one describing the finish or comfort quality.

Respond with ONLY the JSON object, no other text."""

FEW_SHOT_EXAMPLES = """Example 1 (underwear, high-leg cut, lace, synthetic — no material_id match):
{
  "name": "Розали трусы с высоким вырезом на ноге",
  "description": "Женские трусы с высоким вырезом на ноге и кружевной отделкой. Синтетический материал, элегантный крой, мягкая посадка по фигуре.",
  "material_id": null,
  "material_text": "Синтетика",
  "material_composition": "Состав: синтетика, кружевная отделка",
  "planting_type_id": 45007,
  "care_text": "Ручная стирка при низкой температуре. Не отбеливать. Не сушить в стиральной машине.",
  "hashtags": "#трусы #женскоебелье #кружевныетрусы #высокаяпосадка #MarksSpencer"
}

Example 2 (underwear set, medium cut, cotton):
{
  "name": "Комплект из 5 трусов Бразилиана с кружевной отделкой",
  "description": "Комплект из 5 пар женских трусов бразилиана из хлопкового трикотажа с кружевным принтом. Дышащий материал, средняя посадка, мягкая эластичная резинка.",
  "material_id": 62174,
  "material_text": "Хлопок",
  "material_composition": "Состав: хлопок, эластан",
  "planting_type_id": 45009,
  "care_text": "Машинная стирка при 30°C. Не отбеливать. Сушить при низкой температуре.",
  "hashtags": "#трусы #женскоебелье #бразилиана #хлопковоебелье #MarksSpencer"
}

Example 3 (tank top, modal — no planting_type_id, not underwear):
{
  "name": "Комплект из 2 маек Flexifit™ без рукавов",
  "description": "Комплект из 2 женских маек без рукавов из мягкого модалового трикотажа. Однотонная расцветка, эластичная посадка без сдавливания.",
  "material_id": 61952,
  "material_text": "Модал",
  "material_composition": "Состав: модал, эластан",
  "care_text": "Машинная стирка при 30°C. Не отбеливать. Сушить при низкой температуре.",
  "hashtags": "#майка #женскоебелье #модал #безрукавов #MarksSpencer"
}"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "material_id": {"type": ["integer", "null"]},
        "material_text": {"type": "string"},
        "material_composition": {"type": "string"},
        "planting_type_id": {"type": ["integer", "null"]},
        "care_text": {"type": "string"},
        "hashtags": {"type": "string"},
    },
    "required": [
        "name", "description", "material_id", "material_text",
        "material_composition", "care_text", "hashtags",
    ],
    "additionalProperties": False,
}

_LATIN_RE = re.compile(r"[A-Za-z]")
_ALLOWED_LATIN_TOKENS = re.compile(r"MarksSpencer|™")


def _has_disallowed_latin(text):
    """True if text contains Latin letters outside the allowed brand/trademark tokens."""
    stripped = _ALLOWED_LATIN_TOKENS.sub("", text)
    return bool(_LATIN_RE.search(stripped))


def _validate_translation(entry, is_underwear):
    """Raises ValueError with a clear reason if the entry violates a hard policy rule.
    Returns nothing on success — caller treats a clean return as pass."""
    if _has_disallowed_latin(entry["name"]):
        raise ValueError(f"name contains Latin characters: {entry['name']!r}")
    if _has_disallowed_latin(entry["description"]):
        raise ValueError(f"description contains Latin characters: {entry['description']!r}")

    banned_phrases = [
        "посетите", "наш сайт", "наш ассортимент", "смотрите", "www.", "http",
        "instagram", "телефон", "звоните", "контакт",
    ]
    lower_desc = entry["description"].lower()
    for phrase in banned_phrases:
        if phrase in lower_desc:
            raise ValueError(f"description appears to violate content policy (found {phrase!r}): {entry['description']!r}")

    if entry.get("material_id") is not None:
        if entry["material_id"] not in KNOWN_MATERIAL_IDS.values():
            raise ValueError(f"material_id {entry['material_id']!r} is not a known verified dictionary value")

    if not is_underwear and entry.get("planting_type_id") is not None:
        raise ValueError("planting_type_id set on a non-underwear product — must be null")

    if is_underwear and entry.get("planting_type_id") is not None:
        valid = {PLANTING_TYPE_HIGH, PLANTING_TYPE_MEDIUM, PLANTING_TYPE_LOW}
        if entry["planting_type_id"] not in valid:
            raise ValueError(f"planting_type_id {entry['planting_type_id']!r} is not a known verified value")


def generate_translation(article_code, name, specs_json, client=None):
    """Calls the Anthropic API to generate one translation entry for a product.
    Returns (entry_dict, None) on success, or (None, error_reason) on failure —
    caller should skip (never guess) on failure, same policy as the rest of the pipeline."""
    client = client or anthropic.Anthropic()

    category_id, type_id = resolve_category_and_type(name, False)
    if category_id is None:
        return None, (
            f"Could not determine Ozon category from product name {name!r} "
            "(no 'kulot'/'atlet' keyword match) — cannot translate without knowing the category."
        )
    is_underwear = category_id == 200001517  # ozon_mapping.CATEGORY_ID

    user_prompt = (
        f"{FEW_SHOT_EXAMPLES}\n\n"
        f"Now generate the entry for this product:\n"
        f"Source name (Turkish): {name}\n"
        f"Scraped specs (JSON, may include Turkish field names): {specs_json}\n"
        f"Category: {'underwear' if is_underwear else 'tank top / other clothing'}\n\n"
        "Respond with only the JSON object."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        return None, f"Anthropic API call failed: {e}"

    if response.stop_reason == "refusal":
        return None, "Model refused to generate this translation (safety classifier) — skipping, needs manual review."

    text_block = next((b for b in response.content if b.type == "text"), None)
    if text_block is None:
        return None, "No text content in model response."

    try:
        entry = json.loads(text_block.text)
    except json.JSONDecodeError as e:
        return None, f"Model output was not valid JSON: {e}"

    try:
        _validate_translation(entry, is_underwear)
    except ValueError as e:
        return None, f"Generated translation failed policy validation: {e}"

    if not is_underwear:
        entry.pop("planting_type_id", None)

    return entry, None


def append_translation_to_file(article_code, entry, path="ozon_translations.py"):
    """Appends one new entry to the PRODUCT_TRANSLATIONS dict in ozon_translations.py,
    right before the dict's closing brace, formatted to match the existing style."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    marker = "\n}\n\nBRAND_PREFIX"
    if marker not in content:
        raise RuntimeError(
            f"Could not find the expected insertion point in {path} — "
            "file structure may have changed; refusing to guess where to insert."
        )

    lines = [f'    "{article_code}": {{']
    lines.append(f'        "name": {json.dumps(entry["name"], ensure_ascii=False)},')
    lines.append(f'        "description": {json.dumps(entry["description"], ensure_ascii=False)},')
    material_id_repr = entry["material_id"] if entry["material_id"] is not None else "None"
    lines.append(f'        "material_id": {material_id_repr},')
    lines.append(f'        "material_text": {json.dumps(entry["material_text"], ensure_ascii=False)},')
    lines.append(f'        "material_composition": {json.dumps(entry["material_composition"], ensure_ascii=False)},')
    if entry.get("planting_type_id") is not None:
        lines.append(f'        "planting_type_id": {entry["planting_type_id"]},')
    lines.append(f'        "care_text": {json.dumps(entry["care_text"], ensure_ascii=False)},')
    lines.append(f'        "hashtags": {json.dumps(entry["hashtags"], ensure_ascii=False)},')
    lines.append("    },")
    new_entry_text = "\n".join(lines) + "\n"

    updated = content.replace(marker, "\n" + new_entry_text + "}\n\nBRAND_PREFIX", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)


def main():
    """Standalone entry point: finds untranslated products via find_untranslated logic,
    generates + appends a translation for each, auto-translated ones only."""
    import pandas as pd

    from ozon_translations import PRODUCT_TRANSLATIONS

    try:
        df = pd.read_csv("products.csv", encoding="utf-8-sig")
    except FileNotFoundError:
        return print("products.csv not found. Run crawler.py first.")

    unique = df.drop_duplicates("ms_article_code")
    untranslated = unique[~unique["ms_article_code"].isin(PRODUCT_TRANSLATIONS.keys())]
    untranslated = untranslated[untranslated["ms_article_code"].notna()]

    if untranslated.empty:
        print("All crawled products already have a translation on file.")
        return

    print(f"{len(untranslated)} product(s) need translation. Calling Anthropic API...\n")

    client = anthropic.Anthropic()
    translated, skipped = [], []
    for _, row in untranslated.iterrows():
        article_code = row["ms_article_code"]
        entry, error = generate_translation(article_code, row["name"], row["specs_json"], client=client)
        if error:
            print(f"SKIP {article_code} ({row['name']}): {error}")
            skipped.append((article_code, row["name"], error))
            continue
        append_translation_to_file(article_code, entry)
        print(f"OK   {article_code}: {entry['name']}")
        translated.append(article_code)

    print(f"\n{len(translated)} translated and appended to ozon_translations.py, {len(skipped)} skipped.")
    if skipped:
        print("Skipped (need manual attention):")
        for code, name, error in skipped:
            print(f"  {code} ({name}): {error}")


if __name__ == "__main__":
    main()
