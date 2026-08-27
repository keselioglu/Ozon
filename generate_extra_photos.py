"""
Generates additional product photos by cropping/zooming existing M&S photos,
for products that don't have 8 real photos (business instruction, 2026-08-26:
"if there is not enough photos in M&S, zoom existing photos and create
alternative pictures and add them to the end of product photos").

Confirmed on real data (2026-08-26): most crawled products only have 3-6
unique real photos on M&S's own page — this is a genuine content gap on
M&S's side, not a crawler bug (checked the full raw page HTML for a sample
product; only resized/cropped variants of the same handful of source images
exist). Padding with real-photo crops is the only source of "alternative"
imagery available without commissioning new photography.

NOT YET WIRED INTO upload_to_ozon.py — generated crops need to be hosted at
a public URL before Ozon's /v3/product/import can reference them (Ozon
fetches from the URL you give it; it doesn't accept file uploads through
this endpoint), and this pipeline has no image-hosting mechanism yet. This
script only produces local files today; wiring them into the live pipeline
is a follow-up once hosting is set up.

Crop strategy: for each product with fewer than TARGET_PHOTO_COUNT real
photos, cycle through its existing photos generating center-zoom crops at
decreasing crop ratios (each one zooms in further on the garment), until
the target count is reached. This keeps every generated image a genuine
close-up of the real product, never a fabricated one.
"""
import os
import sys
from io import BytesIO

import requests
from PIL import Image

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

PRODUCTS_CSV = "products.csv"
OUTPUT_DIR = "generated_photos"
TARGET_PHOTO_COUNT = 8

# Each successive crop zooms in further on the image center — ratio is the
# fraction of the original width/height kept, centered. 1.0 would be the
# full original (never generated, since that's already a real photo).
ZOOM_CROP_RATIOS = [0.75, 0.6, 0.45, 0.35]


def download_image(url, timeout=15):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGB")


def center_crop_zoom(img, ratio):
    """Crops to `ratio` of the image centered, then resizes back up to the
    original dimensions so the result reads as a zoomed-in shot, not just a
    smaller image."""
    w, h = img.size
    crop_w, crop_h = int(w * ratio), int(h * ratio)
    left = (w - crop_w) // 2
    top = (h - crop_h) // 2
    cropped = img.crop((left, top, left + crop_w, top + crop_h))
    return cropped.resize((w, h), Image.LANCZOS)


def generate_extra_photos_for_product(article_code, image_urls, output_dir):
    """Downloads the product's real photos and generates ONE zoom crop per
    source photo (business instruction, 2026-08-27: "not the zoom of same
    photo use different photos to zoom" -- an earlier version cycled through
    multiple zoom ratios on the SAME photo once the real-photo pool ran out,
    which produced near-duplicate images for low-photo-count products).
    Returns the list of generated local file paths -- may be SHORTER than
    `needed` if the product doesn't have enough distinct real photos to
    cover the gap; that's a hard ceiling now, not something to paper over by
    reusing a photo at a different zoom depth."""
    real_count = len(image_urls)
    needed = TARGET_PHOTO_COUNT - real_count
    if needed <= 0 or not image_urls:
        return []

    product_dir = os.path.join(output_dir, article_code)
    os.makedirs(product_dir, exist_ok=True)

    generated_paths = []
    # Each source photo is used for AT MOST one generated crop -- distinct
    # photos are exhausted before we'd ever consider a second ratio on the
    # same one, and since we cap at one-per-photo, no second ratio is used
    # at all (also avoids near-duplicate crops of the same garment shot).
    for url, ratio in zip(image_urls, ZOOM_CROP_RATIOS):
        if len(generated_paths) >= needed:
            break
        try:
            img = download_image(url)
        except Exception as e:
            print(f"    ! could not download {url}: {e}")
            continue

        zoomed = center_crop_zoom(img, ratio)
        out_path = os.path.join(product_dir, f"zoom_{len(generated_paths) + 1}.jpg")
        zoomed.save(out_path, "JPEG", quality=90)
        generated_paths.append(out_path)

    if len(generated_paths) < needed:
        print(f"    ! only {len(generated_paths)}/{needed} crop(s) generated -- "
              f"not enough distinct real photos ({real_count}) to reach {TARGET_PHOTO_COUNT} without reusing one.")

    return generated_paths


def main():
    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    unique = df.drop_duplicates("ms_article_code")
    total_needing = 0
    total_generated = 0

    for _, row in unique.iterrows():
        article_code = row.get("ms_article_code")
        if pd.isna(article_code):
            continue

        image_urls = [u.strip() for u in str(row.get("image_urls") or "").split("|") if u.strip()]
        real_count = len(image_urls)
        if real_count >= TARGET_PHOTO_COUNT:
            continue

        total_needing += 1
        print(f"{article_code}: {real_count} real photo(s), generating {TARGET_PHOTO_COUNT - real_count} crop(s)...")
        generated = generate_extra_photos_for_product(article_code, image_urls, OUTPUT_DIR)
        total_generated += len(generated)
        print(f"  -> {len(generated)} file(s) written to {OUTPUT_DIR}/{article_code}/")

    print(f"\n{total_needing} product(s) needed extra photos, {total_generated} crop(s) generated total.")
    print(f"Files are local only in {OUTPUT_DIR}/ — not yet uploaded anywhere. "
          "Wiring into upload_to_ozon.py needs a public image host first (see module docstring).")


if __name__ == "__main__":
    main()
