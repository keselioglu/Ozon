"""
Longer product descriptions (Ozon annotation, attribute 4191) for the content job.

Ozon's content rating has a text condition "Аннотация более 500 знаков" (annotation over 500
characters). 9,595 of 11,487 rated M&S offers missed it on 2026-11-25 -- the upload trims the
translated description to 500 characters, so it can never qualify.

build_long_annotation() rewrites the description to 600-1500 characters from facts we already
hold (the on-file Russian translation + the source page's specs), same house rules as
auto_translate.py: Cyrillic only, no brand names, no links / calls to action / other
marketplaces, and NO invented facts (no percentages, certifications or measurements that the
source doesn't state). One retry with the validation error fed back; otherwise None (skip).
"""
import json
import re

from auto_translate import MODEL  # same model as the listing translations
MIN_CHARS, MAX_CHARS = 600, 1500

SYSTEM = """You write the Russian product description (annotation) for an Ozon clothing listing.
Rules -- a listing that breaks them is rejected:
- Russian, Cyrillic letters only: no Latin letters at all (write "клиновидный вырез", not "V-образный").
- Between 600 and 1500 characters.
- Describe only this product: cut, fabric, details, how and when to wear it, care. Use ONLY facts
  from the material you are given. Never invent fibre percentages, certifications, measurements,
  country of origin or warranty. If a fact isn't given, don't state it; describe style and use instead.
- No brand names, no links, no contact details, no calls to action, no other marketplaces, no
  comparisons with other brands.
Respond with only the description text."""

_LATIN = re.compile(r"[A-Za-z]")
_OTHER = re.compile(r"[^Ѐ-ӿ0-9\s.,:;!?()\[\]«»\"'„“”‘’\-–—/%°№+*]")
_BANNED = re.compile(r"https?://|www\.|ozon|wildberries|яндекс|маркетплейс|marks|spencer|h&m", re.IGNORECASE)


def validate(text):
    """Raises ValueError with the reason if `text` breaks a rule."""
    if not isinstance(text, str):
        raise ValueError("no text")
    t = text.strip()
    if not MIN_CHARS <= len(t) <= MAX_CHARS:
        raise ValueError(f"length {len(t)} characters, must be {MIN_CHARS}-{MAX_CHARS}")
    m = _LATIN.search(t)
    if m:
        raise ValueError(f"contains a Latin letter ({t[max(0, m.start() - 15):m.end() + 15]!r})")
    m = _OTHER.search(t)  # e.g. a stray Korean "드" seen in a 2026-11-25 test output
    if m:
        raise ValueError(f"contains a character that is not Cyrillic/digits/punctuation "
                         f"({t[max(0, m.start() - 15):m.end() + 15]!r})")
    m = _BANNED.search(t)
    if m:
        raise ValueError(f"contains a forbidden word ({m.group(0)!r})")
    return t


def build_long_annotation(name, translation, source_facts, client=None):
    """-> (text, None) or (None, reason). translation: the on-file auto_translate entry;
    source_facts: dict/str of the source page's specs (Turkish is fine)."""
    import anthropic
    client = client or anthropic.Anthropic()
    facts = {
        "name (Russian)": name,
        "current description": (translation or {}).get("description"),
        "material": (translation or {}).get("material_composition") or (translation or {}).get("material_text"),
        "care": (translation or {}).get("care_text"),
        "source page specs": source_facts,
    }
    messages = [{"role": "user", "content": "Material:\n" + json.dumps(facts, ensure_ascii=False, indent=1)}]
    for attempt in range(2):
        try:
            r = client.messages.create(model=MODEL, max_tokens=1200, system=SYSTEM, messages=messages)
        except anthropic.APIError as e:
            return None, f"API error: {e}"
        if r.stop_reason == "refusal":
            return None, "refused"
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        try:
            return validate(text), None
        except ValueError as e:
            if attempt == 1:
                return None, f"failed validation: {e}"
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": f"Rejected: {e}. Fix only that and reply with the corrected text."}]
    return None, "unreachable"
