"""
One-off correction: rewrites every product's hashtags to drop the brand name
and be 5 genuine search terms instead (business instruction, 2026-08-26).

Previously every entry in ozon_translations.py ended with the fixed tag
"#MarksSpencer" and had only 4 content hashtags. Per instruction: hashtags
should exclude the brand name entirely and cover 5 potential buyer searches
per product.

CORRECTION (2026-08-27): this script originally also appended the hashtags
into the product description, per the same instruction. Ozon's own PDP
validation rejects that — "the description contains keywords for search,
transfer them to the Keywords field" — since hashtags belong only in the
dedicated hashtags field. build_description_with_hashtags is kept below only
for reference/rollback; main() no longer calls it, and generate_hashtags
alone is what a re-run uses now. All 156 already-affected descriptions were
reverted by hand (see conversation history, 2026-08-27) — this script does
NOT re-apply that fix, since PRODUCT_TRANSLATIONS entries currently in the
file no longer end with their hashtags and don't need touching again.

Uses the Anthropic API (same model/pattern as auto_translate.py) to generate
new hashtags per product from its existing Russian name/description/material
— not re-translating from M&S, just re-deriving search terms from content
already on file. Rewrites ozon_translations.py in place, one entry at a time,
preserving every other field untouched.

Not part of daily_run.py -- a one-time rewrite of the existing catalog.
New products going forward should get correct hashtags directly from
auto_translate.py (see the matching prompt update there).
"""
import json
import re
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from ozon_translations import PRODUCT_TRANSLATIONS

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You generate Russian-language search hashtags for Ozon marketplace listings (women's \
underwear and loungewear).

You will be given a product's existing Russian name, description, and material. Generate exactly 5 lowercase \
Russian hashtags that real buyers would plausibly search for on Ozon when looking for this kind of product \
(garment type, material, fit/cut, style features, occasion).

Hard rules:
- Exactly 5 hashtags, space-separated, each starting with #, no spaces within a tag, all lowercase Cyrillic.
- Do NOT include the brand name (no "MarksSpencer", no "marks", no "spencer" in any form, in any tag).
- Do NOT repeat the exact same tag across very similar products — vary wording where the product genuinely differs \
(e.g. "с кружевом" vs "кружевные" is fine to vary), but always describe what's actually true about THIS product.
- Base tags only on facts already present in the given name/description/material — do not invent features.
- Prefer terms an actual shopper would type into search: garment type (трусы, майка), material (хлопок, вискоза, \
модал), fit (высокая посадка, бразилиана, бесшовные), and one broader category term (женское белье, домашняя одежда).

Respond with ONLY the JSON object {"hashtags": "..."}, no other text."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"hashtags": {"type": "string"}},
    "required": ["hashtags"],
    "additionalProperties": False,
}

BRAND_WORDS_RE = re.compile(r"marks|spencer|марк|спенсер", re.IGNORECASE)
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def validate_hashtags(hashtags):
    tags = hashtags.split()
    if len(tags) != 5:
        return f"expected exactly 5 hashtags, got {len(tags)}: {hashtags!r}"
    for tag in tags:
        if not tag.startswith("#"):
            return f"tag {tag!r} does not start with #"
        if BRAND_WORDS_RE.search(tag):
            return f"tag {tag!r} contains a brand reference — not allowed"
        # Confirmed on real output (2026-08-26): the model occasionally slips
        # a single Latin lookalike letter into an otherwise-Cyrillic word
        # (e.g. "#трусысkружевом" with a Latin k) — a real Ozon content-policy
        # violation (Latin characters aren't allowed) that a length/brand-only
        # check doesn't catch. Reject and regenerate rather than ship it.
        if LATIN_LETTER_RE.search(tag):
            return f"tag {tag!r} contains a Latin character — not allowed"
    return None


def generate_hashtags(article_code, entry, client):
    user_prompt = (
        f"Product name: {entry['name']}\n"
        f"Description: {entry['description']}\n"
        f"Material: {entry.get('material_text', 'unknown')}\n\n"
        "Generate the 5 hashtags."
    )
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        return None, f"API call failed: {e}"

    if response.stop_reason == "refusal":
        return None, "model refused"

    text_block = next((b for b in response.content if b.type == "text"), None)
    if text_block is None:
        return None, "no text content in response"

    try:
        result = json.loads(text_block.text)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"

    hashtags = result.get("hashtags", "")
    error = validate_hashtags(hashtags)
    if error:
        return None, error

    return hashtags, None


def build_description_with_hashtags(description, hashtags):
    """Appends the hashtags to the description, respecting the 500-char cap
    applied at upload time (see ATTR_ANNOTATION truncation in
    upload_to_ozon.py) -- trims the description text itself if needed so the
    hashtags always survive the truncation, rather than risk them being cut
    off the end silently."""
    combined = f"{description} {hashtags}"
    if len(combined) <= 500:
        return combined
    room_for_description = 500 - len(hashtags) - 1
    trimmed = description[:room_for_description].rstrip()
    return f"{trimmed} {hashtags}"


def main():
    client = anthropic.Anthropic()
    path = "ozon_translations.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    updated, skipped, already_ok = [], [], []
    for article_code, entry in PRODUCT_TRANSLATIONS.items():
        # Re-runnable: skip entries that already pass validation (no brand
        # reference, exactly 5 tags, no stray Latin, not duplicated into the
        # description) so a re-run only touches what still needs fixing,
        # instead of regenerating already-correct hashtags with different
        # (non-deterministic) output.
        if (validate_hashtags(entry["hashtags"]) is None
                and not entry["description"].rstrip().endswith(entry["hashtags"])):
            already_ok.append(article_code)
            continue

        hashtags, error = generate_hashtags(article_code, entry, client)
        if error:
            print(f"SKIP {article_code}: {error}")
            skipped.append((article_code, error))
            continue

        old_hashtags_line = f'"hashtags": {json.dumps(entry["hashtags"], ensure_ascii=False)},'
        new_hashtags_line = f'"hashtags": {json.dumps(hashtags, ensure_ascii=False)},'

        if old_hashtags_line not in content:
            print(f"SKIP {article_code}: could not find exact hashtags line to replace — file may have "
                  f"been reformatted; refusing to guess.")
            skipped.append((article_code, "hashtags line not found verbatim"))
            continue

        content = content.replace(old_hashtags_line, new_hashtags_line, 1)

        # If this entry's description still has the hashtags appended (an
        # already-fixed hashtags field with a stale description, e.g. from
        # before the 2026-08-27 correction), strip them — description must
        # never contain the hashtags, see module docstring.
        if entry["description"].rstrip().endswith(entry["hashtags"]):
            trimmed_description = entry["description"].rstrip()[: -len(entry["hashtags"])].rstrip()
            old_description_line = f'"description": {json.dumps(entry["description"], ensure_ascii=False)},'
            new_description_line = f'"description": {json.dumps(trimmed_description, ensure_ascii=False)},'
            if old_description_line in content:
                content = content.replace(old_description_line, new_description_line, 1)

        print(f"OK   {article_code}: {hashtags}")
        updated.append(article_code)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n{len(updated)} product(s) updated, {len(skipped)} skipped, "
          f"{len(already_ok)} already correct (untouched).")
    if skipped:
        print("Skipped (need manual attention):")
        for code, error in skipped:
            print(f"  {code}: {error}")


if __name__ == "__main__":
    main()
