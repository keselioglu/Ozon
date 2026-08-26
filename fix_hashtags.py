"""
One-off correction: rewrites every product's hashtags to drop the brand name
and be 5 genuine search terms instead, then appends those hashtags into the
product description too (business instruction, 2026-08-26).

Previously every entry in ozon_translations.py ended with the fixed tag
"#MarksSpencer" and had only 4 content hashtags. Per instruction: hashtags
should exclude brand name entirely and include 5 potential buyer searches
per product; the same 5 tags should also appear in the description.

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
        # reference, exactly 5 tags, no stray Latin) so a re-run after a
        # partial failure (e.g. API credits ran out mid-run, 2026-08-26)
        # only touches what still needs fixing, instead of regenerating
        # already-correct hashtags with different (non-deterministic) output.
        if validate_hashtags(entry["hashtags"]) is None and entry["description"].rstrip().endswith(entry["hashtags"]):
            already_ok.append(article_code)
            continue

        hashtags, error = generate_hashtags(article_code, entry, client)
        if error:
            print(f"SKIP {article_code}: {error}")
            skipped.append((article_code, error))
            continue

        new_description = build_description_with_hashtags(entry["description"], hashtags)

        old_hashtags_line = f'"hashtags": {json.dumps(entry["hashtags"], ensure_ascii=False)},'
        new_hashtags_line = f'"hashtags": {json.dumps(hashtags, ensure_ascii=False)},'

        if old_hashtags_line not in content:
            print(f"SKIP {article_code}: could not find exact hashtags line to replace — file may have "
                  f"been reformatted; refusing to guess.")
            skipped.append((article_code, "hashtags line not found verbatim"))
            continue

        # Description isn't always a single-line string — the original 24
        # hand-written entries use Python's implicit string concatenation
        # across multiple lines: "description": (\n    "..." \n    "..." \n),
        # while auto_translate.py-generated entries are single-line. Match
        # the whole "description": <value>, field within THIS article's dict
        # block (bounded by the next top-level '"<CODE>": {' or end of dict)
        # rather than assuming either shape, so both are handled correctly.
        block_start = content.index(f'"{article_code}": {{')
        next_entry_match = re.search(r'\n {4}"[^"]+": \{', content[block_start + 1:])
        block_end = block_start + 1 + next_entry_match.start() if next_entry_match else len(content)
        block = content[block_start:block_end]

        description_field_re = re.compile(r'"description":\s*(".*?"|\(.*?\)),', re.DOTALL)
        m = description_field_re.search(block)
        if not m:
            print(f"SKIP {article_code}: could not find a description field in this entry's block.")
            skipped.append((article_code, "description field not found in block"))
            continue

        new_description_field = f'"description": {json.dumps(new_description, ensure_ascii=False)},'
        new_block = block[:m.start()] + new_description_field + block[m.end():]
        new_block = new_block.replace(old_hashtags_line, new_hashtags_line, 1)

        content = content[:block_start] + new_block + content[block_end:]

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
