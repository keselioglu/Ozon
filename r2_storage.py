"""
Uploads locally-generated files (product videos, photo crops, size-chart
images, rich-content assets) to Cloudflare R2, so they get a public URL Ozon
can fetch from -- Ozon's API is URL-only everywhere in this pipeline (product
images, video links, rich content, size charts all require an already-public
URL; confirmed 2026-08-27, no direct file-upload endpoint exists anywhere in
the product content API). R2 was chosen over Google Drive (share links
aren't direct-fetchable URLs, and Google throttles high-volume automated
access) for cost (no egress fees) and reliability at catalog scale.

R2 is S3-compatible, so this uses boto3's S3 client pointed at R2's
account-specific endpoint rather than any R2-specific SDK.

Required .env vars (same pattern as OZON_API_KEY / ANTHROPIC_API_KEY):
    R2_ACCOUNT_ID        -- Cloudflare account ID
    R2_ACCESS_KEY_ID      -- R2 API token's access key
    R2_SECRET_ACCESS_KEY  -- R2 API token's secret key
    R2_BUCKET_NAME        -- the bucket generated media gets uploaded to
    R2_PUBLIC_BASE_URL    -- the bucket's public URL (r2.dev subdomain or a
                             custom domain) -- objects are reachable at
                             {R2_PUBLIC_BASE_URL}/{object_key}
"""
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import boto3
from dotenv import load_dotenv

load_dotenv()

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL")

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def is_configured():
    return all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL])


def _client():
    if not is_configured():
        missing = [name for name, val in [
            ("R2_ACCOUNT_ID", R2_ACCOUNT_ID), ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID),
            ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY), ("R2_BUCKET_NAME", R2_BUCKET_NAME),
            ("R2_PUBLIC_BASE_URL", R2_PUBLIC_BASE_URL),
        ] if not val]
        raise RuntimeError(f"R2 not configured -- missing .env var(s): {', '.join(missing)}")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def upload_file(local_path, object_key):
    """Uploads local_path to R2 under object_key (e.g.
    "videos/T81006849L/video.mp4") and returns its public URL. Overwrites
    any existing object at that key -- callers wanting to skip re-uploads
    should check that themselves (e.g. via a local manifest), since R2
    doesn't expose a cheap "does this exist" check worth calling per file."""
    client = _client()
    ext = os.path.splitext(local_path)[1].lower()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

    client.upload_file(
        local_path, R2_BUCKET_NAME, object_key,
        ExtraArgs={"ContentType": content_type},
    )
    return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"


def upload_directory(local_dir, key_prefix):
    """Uploads every file directly inside local_dir (non-recursive) under
    key_prefix, returning {filename: public_url}."""
    urls = {}
    for filename in sorted(os.listdir(local_dir)):
        local_path = os.path.join(local_dir, filename)
        if not os.path.isfile(local_path):
            continue
        object_key = f"{key_prefix.rstrip('/')}/{filename}"
        urls[filename] = upload_file(local_path, object_key)
    return urls


if __name__ == "__main__":
    if not is_configured():
        print("R2 is not configured yet. Add these to .env:")
        print("  R2_ACCOUNT_ID=")
        print("  R2_ACCESS_KEY_ID=")
        print("  R2_SECRET_ACCESS_KEY=")
        print("  R2_BUCKET_NAME=")
        print("  R2_PUBLIC_BASE_URL=")
    else:
        print("R2 is configured. Bucket:", R2_BUCKET_NAME, "| Public base URL:", R2_PUBLIC_BASE_URL)
