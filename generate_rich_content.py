"""
Builds Ozon rich-content JSON (attribute 11254, "Rich-контент JSON") per
product, using 100%-original Russian wording generated from the product's
own on-file translation data plus its real M&S photos (business instruction,
2026-08-26/27, GitHub issue #7: "using a default picture from marks and
spencer main page or category page, use marks and spencer logo, use 100%
original wording").

Schema (corroborated 2026-08-27 via community sources -- Ozon's own docs
pages redirect-loop for automated fetching, so this is NOT verified against
an official example; confirm against a real submission before trusting it
at scale):
    {"content": [ <widget>, ... ], "version": 0.3}
Widgets are "ra"-prefixed (raTextBlock, raShowcase, raCoverImage, ...), each
with its own nested shape. Only raTextBlock's shape is corroborated by an
independent, runnable source (a GitHub Bitrix module) -- raShowcase's
image-block shape below was reconstructed from search snippets only and
should be treated as a best-effort guess pending a real test submission.

Content strategy per product (100% original wording -- never copies M&S's
own listing text):
  1. raTextBlock: a short original overview paragraph, written fresh from
     the product's material/care facts already on file in
     ozon_translations.py (not the M&S description, which lives in
     products.csv and is never used here).
  2. raShowcase (image + text): one of the product's own real M&S photos
     (already hosted on M&S's CDN in products.csv's image_urls -- these
     ARE already public URLs, so unlike generated crops/videos/charts this
     part has NO hosting blocker) paired with an original caption about
     material/fit.
  3. raTextBlock: care instructions, original wording.

M&S's logo is NOT included as a rich-content image block: M&S's own site
does not expose a standalone logo image URL suitable for reuse (it's CSS/
sprite-based on their site, confirmed by inspection), and downloading +
re-hosting their logo would need the same image-hosting solution as the
photo-crop/video/size-chart-image tasks (issues #4/#5/#8) -- once that
hosting exists, a logo block can be added the same way.

NOT part of daily_run.py -- a one-time content build. Pushing to live
listings is a separate follow-up (build_rich_content_for_article below
returns the JSON string; nothing here calls /v3/product/import).
"""
import json
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from ozon_translations import PRODUCT_TRANSLATIONS

RICH_CONTENT_VERSION = 0.3
PRODUCTS_CSV = "products.csv"
OUTPUT_FILE = "generated_rich_content.jsonl"


def build_overview_text(translation):
    """Original paragraph (not M&S's own copy) from facts already on file:
    material + fit/planting type. Deliberately does not reuse
    translation["description"], which is itself original but written for
    the plain Annotation field -- rich content gets its own distinct
    wording so the PDP doesn't just repeat the same sentence twice."""
    material = translation.get("material_text", "").strip()
    parts = []
    if material:
        parts.append(f"Материал: {material.lower()}.")
    parts.append("Продуманный крой обеспечивает комфортную посадку и мягкое прилегание к телу в течение всего дня.")
    return " ".join(parts)


def build_care_text(translation):
    care = translation.get("care_text", "").strip()
    if not care:
        return None
    return f"Рекомендации по уходу: {care}"


def build_rich_content(translation, primary_image_url):
    """raShowcase's "chess" type alternates image/text blocks at a
    contained ~708x708 size (confirmed live, 2026-08-27, after an initial
    version rendered full-page-width -- the widget expects 2+ blocks with
    this exact size and a "reverse" flag per block, not a single block with
    arbitrary width/height values, which is what caused the full-bleed
    rendering business flagged)."""
    content = [
        {
            "widgetName": "raTextBlock",
            "text": {
                "size": "size2",
                "color": "color1",
                "content": [build_overview_text(translation)],
            },
        },
    ]

    if primary_image_url:
        care_text = build_care_text(translation) or ""
        content.append({
            "widgetName": "raShowcase",
            "type": "chess",
            "blocks": [
                {
                    "img": {
                        "src": primary_image_url,
                        "srcMobile": primary_image_url,
                        "alt": translation.get("name", ""),
                        "width": 708,
                        "height": 708,
                        "widthMobile": 640,
                        "heightMobile": 640,
                    },
                    "title": {"content": [translation.get("name", "")]},
                    "text": {
                        "size": "size2",
                        "align": "left",
                        "color": "color1",
                        "content": [build_overview_text(translation)],
                    },
                    "reverse": False,
                },
                {
                    "img": {
                        "src": primary_image_url,
                        "srcMobile": primary_image_url,
                        "alt": translation.get("name", ""),
                        "width": 708,
                        "height": 708,
                        "widthMobile": 640,
                        "heightMobile": 640,
                    },
                    "title": {"content": ["Уход за изделием"]},
                    "text": {
                        "size": "size2",
                        "align": "left",
                        "color": "color1",
                        "content": [care_text],
                    },
                    "reverse": True,
                },
            ],
        })
        return {"content": content, "version": RICH_CONTENT_VERSION}

    care_text = build_care_text(translation)
    if care_text:
        content.append({
            "widgetName": "raTextBlock",
            "text": {
                "size": "size2",
                "color": "color1",
                "content": [care_text],
            },
        })

    return {"content": content, "version": RICH_CONTENT_VERSION}


def load_primary_images():
    """article_code -> first real M&S photo URL, from products.csv (already
    public on M&S's own CDN -- no hosting blocker for this specific image)."""
    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    images = {}
    for _, row in df.drop_duplicates("ms_article_code").iterrows():
        article_code = row.get("ms_article_code")
        urls = str(row.get("image_urls") or "").split("|")
        urls = [u.strip() for u in urls if u.strip()]
        if article_code and urls:
            images[article_code] = urls[0]
    return images


def main():
    primary_images = load_primary_images()
    print(f"{len(primary_images)} article code(s) have a known primary M&S photo.\n")

    built = 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for article_code, translation in PRODUCT_TRANSLATIONS.items():
            image_url = primary_images.get(article_code)
            rich_content = build_rich_content(translation, image_url)
            f.write(json.dumps({
                "article_code": article_code,
                "rich_content": rich_content,
            }, ensure_ascii=False) + "\n")
            built += 1

    print(f"{built} rich-content JSON payload(s) built, saved to {OUTPUT_FILE}.")
    print("NOT pushed to Ozon yet -- schema is only community-corroborated, not confirmed against "
          "an official example. Recommend testing one payload against a single live offer_id via "
          "/v3/product/import first (check the response for attribute-level validation errors) "
          "before pushing catalog-wide.")


if __name__ == "__main__":
    main()
