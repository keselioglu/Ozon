"""
Generates a short pan/zoom (Ken Burns style) slideshow video per product from
its existing real M&S photos, plus a cover image, for GitHub issue #4
(business instruction: "combine product pictures into a short video and add
to product page and add a video cover").

This only does the LOCAL generation step -- downloading photos and running
ffmpeg. Pushing the result live needs a public host for the generated .mp4/
.jpg files, since Ozon's video attributes (21841 "Ozon.Video: link", 21845
"Ozon.Video cover: link") are plain URL fields Ozon fetches from, not file
uploads -- same blocker as generate_extra_photos.py (issue #8) and the size
chart images (issue #5). This script produces local files only; uploading +
pushing the URLs to Ozon is a follow-up once hosting is decided.

Video approach: each photo gets a few seconds with a slow zoom-in (Ken
Burns effect via ffmpeg's zoompan filter), photos are concatenated in
sequence, output as an H.264 MP4. Keeps every frame a genuine real M&S
photo -- nothing fabricated, consistent with how generate_extra_photos.py
only ever crops real source images.
"""
import os
import subprocess
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import requests

PRODUCTS_CSV = "products.csv"
OUTPUT_DIR = "generated_videos"
MAX_PHOTOS_PER_VIDEO = 6
SECONDS_PER_PHOTO = 2.5
FPS = 30
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1080  # square -- safest aspect ratio for marketplace video widgets


def download_image(url, out_path, timeout=15):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)


def build_video_for_product(article_code, image_urls, output_dir):
    """Downloads up to MAX_PHOTOS_PER_VIDEO real photos and renders a
    pan/zoom slideshow MP4 + a cover JPG (the first photo). Returns
    (video_path, cover_path) or (None, None) if there were no usable photos."""
    photos = image_urls[:MAX_PHOTOS_PER_VIDEO]
    if not photos:
        return None, None

    product_dir = os.path.join(output_dir, article_code)
    os.makedirs(product_dir, exist_ok=True)

    local_photos = []
    for i, url in enumerate(photos):
        local_path = os.path.join(product_dir, f"src_{i}.jpg")
        try:
            download_image(url, local_path)
            local_photos.append(local_path)
        except Exception as e:
            print(f"    ! could not download {url}: {e}")

    if not local_photos:
        return None, None

    cover_path = os.path.join(product_dir, "cover.jpg")
    with open(local_photos[0], "rb") as src, open(cover_path, "wb") as dst:
        dst.write(src.read())

    video_path = os.path.join(product_dir, "video.mp4")
    total_frames = int(SECONDS_PER_PHOTO * FPS)

    # Build one ffmpeg filter_complex chain: each input gets scaled/cropped
    # to a square canvas, then zoompan applies a slow, steady zoom-in over
    # its duration; xfade would be nicer for cross-dissolves but zoompan
    # already reads as intentional motion and keeps the filter graph simple
    # and robust across an arbitrary photo count.
    # zoompan re-evaluates its zoom expression once per INPUT frame it
    # receives, not once per output frame -- feeding it a fast loop source
    # makes it multiply far more output frames than `d` alone implies (a
    # well-known ffmpeg gotcha, confirmed the hard way across 3 failed
    # attempts: default-rate "-loop 1 -t N" produced a runaway 90MB
    # unterminated file; "-r 1 -t N" and a post-hoc "fps=1" filter each
    # still measured 3 input frames via ffprobe/frame-count for a 2.5s
    # segment, tripling every segment's actual duration, because -t's
    # default-rate rounding doesn't go away just by filtering afterward).
    # The fix that actually works: pin the INPUT's own framerate to exactly
    # 1 via "-framerate 1" and cap it to exactly "-t 1" (one second is
    # enough for a single still frame, since zoompan re-stretches it to the
    # real segment length via `d` regardless of the 1s source duration) --
    # verified via ffprobe to yield exactly 1 input frame.
    inputs = []
    filters = []
    for i, photo in enumerate(local_photos):
        inputs += ["-loop", "1", "-framerate", "1", "-t", "1", "-i", photo]
        filters.append(
            f"[{i}:v]scale={OUTPUT_WIDTH * 2}:{OUTPUT_HEIGHT * 2}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH * 2}:{OUTPUT_HEIGHT * 2},"
            f"zoompan=z='min(zoom+0.0015,1.3)':d={total_frames}:s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:fps={FPS}[v{i}]"
        )

    concat_inputs = "".join(f"[v{i}]" for i in range(len(local_photos)))
    filter_complex = ";".join(filters) + f";{concat_inputs}concat=n={len(local_photos)}:v=1:a=0[outv]"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ! ffmpeg failed: {result.stderr[-800:]}")
        return None, cover_path

    # Clean up downloaded source frames -- only the final video/cover are needed.
    for photo in local_photos:
        os.remove(photo)

    return video_path, cover_path


def main():
    try:
        df = pd.read_csv(PRODUCTS_CSV, encoding="utf-8-sig")
    except FileNotFoundError:
        return print(f"{PRODUCTS_CSV} not found.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    unique = df.drop_duplicates("ms_article_code")

    total, ok, failed = 0, 0, 0
    for _, row in unique.iterrows():
        article_code = row.get("ms_article_code")
        if pd.isna(article_code):
            continue
        image_urls = [u.strip() for u in str(row.get("image_urls") or "").split("|") if u.strip()]
        if not image_urls:
            continue

        total += 1
        print(f"{article_code}: building video from {min(len(image_urls), MAX_PHOTOS_PER_VIDEO)} photo(s)...")
        video_path, cover_path = build_video_for_product(article_code, image_urls, OUTPUT_DIR)
        if video_path:
            ok += 1
            print(f"  -> {video_path}")
        else:
            failed += 1
            print(f"  ! failed")

    print(f"\n{total} product(s) processed, {ok} video(s) generated, {failed} failed.")
    print(f"Files are local only in {OUTPUT_DIR}/ -- not yet uploaded anywhere. "
          "Needs a public host before pushing to Ozon (attributes 21841/21845), see issue #4.")


if __name__ == "__main__":
    main()
