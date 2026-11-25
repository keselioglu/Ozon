"""
Uploads locally-generated files (product videos, photo crops, size-chart images, rich-content
assets) to S3-compatible object storage, so they get a public URL Ozon can fetch from -- Ozon's
API is URL-only everywhere in this pipeline (no direct file-upload endpoint exists in the
product content API; confirmed 2026-08-27).

Two providers, picked from .env (the first one fully configured wins):

  1. MEDIA_S3_* -- any S3-compatible store; on this PC Yandex Object Storage (business decision
     2026-11-25: Ozon could NOT download from Cloudflare's r2.dev address from Russia -- photos
     were silently dropped and the video link removed; Yandex is reachable from Russia):
         MEDIA_S3_ENDPOINT      e.g. https://storage.yandexcloud.net
         MEDIA_S3_BUCKET        bucket name (public read)
         MEDIA_S3_KEY_ID        static access key id
         MEDIA_S3_SECRET        static access key secret
         MEDIA_PUBLIC_BASE_URL  e.g. https://storage.yandexcloud.net/<bucket>
         MEDIA_S3_REGION        optional, default ru-central1
  2. R2_* -- Cloudflare R2 (the original setup):
         R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL

Objects are reachable at {PUBLIC_BASE_URL}/{object_key}. The module name stays r2_storage so
existing imports keep working.
"""
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import boto3
from dotenv import load_dotenv

load_dotenv()

_MEDIA = {k: os.getenv(k) for k in ("MEDIA_S3_ENDPOINT", "MEDIA_S3_BUCKET", "MEDIA_S3_KEY_ID",
                                    "MEDIA_S3_SECRET", "MEDIA_PUBLIC_BASE_URL")}
_R2 = {k: os.getenv(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                                 "R2_BUCKET_NAME", "R2_PUBLIC_BASE_URL")}

if all(_MEDIA.values()):
    PROVIDER = "s3"
    ENDPOINT = _MEDIA["MEDIA_S3_ENDPOINT"].rstrip("/")
    BUCKET = _MEDIA["MEDIA_S3_BUCKET"]
    KEY_ID, SECRET = _MEDIA["MEDIA_S3_KEY_ID"], _MEDIA["MEDIA_S3_SECRET"]
    PUBLIC_BASE_URL = _MEDIA["MEDIA_PUBLIC_BASE_URL"].rstrip("/")
    REGION = os.getenv("MEDIA_S3_REGION") or "ru-central1"
elif all(_R2.values()):
    PROVIDER = "r2"
    ENDPOINT = f"https://{_R2['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    BUCKET = _R2["R2_BUCKET_NAME"]
    KEY_ID, SECRET = _R2["R2_ACCESS_KEY_ID"], _R2["R2_SECRET_ACCESS_KEY"]
    PUBLIC_BASE_URL = _R2["R2_PUBLIC_BASE_URL"].rstrip("/")
    REGION = "auto"
else:
    PROVIDER = ENDPOINT = BUCKET = KEY_ID = SECRET = PUBLIC_BASE_URL = REGION = None

# Old names, still imported elsewhere.
R2_BUCKET_NAME = BUCKET
R2_PUBLIC_BASE_URL = PUBLIC_BASE_URL

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def is_configured():
    return PROVIDER is not None


def client():
    if not is_configured():
        raise RuntimeError("Media storage not configured -- set MEDIA_S3_* (Yandex) or R2_* in .env")
    return boto3.client("s3", endpoint_url=ENDPOINT, aws_access_key_id=KEY_ID,
                        aws_secret_access_key=SECRET, region_name=REGION)


_client = client  # old name


def public_url(object_key):
    return f"{PUBLIC_BASE_URL}/{object_key}"


def key_for_url(url):
    """Object key if `url` points into our bucket, else None."""
    if not is_configured() or not isinstance(url, str) or not url.startswith(PUBLIC_BASE_URL + "/"):
        return None
    return url[len(PUBLIC_BASE_URL) + 1:]


def upload_file(local_path, object_key):
    """Uploads local_path under object_key (e.g. "videos/T81006849L/video.mp4") and returns its
    public URL. Overwrites any existing object at that key. Objects are written public-read
    (Yandex needs it per object unless the bucket itself is public; harmless elsewhere)."""
    c = client()
    ext = os.path.splitext(local_path)[1].lower()
    extra = {"ContentType": CONTENT_TYPES.get(ext, "application/octet-stream")}
    if PROVIDER == "s3":
        extra["ACL"] = "public-read"
    c.upload_file(local_path, BUCKET, object_key, ExtraArgs=extra)
    return public_url(object_key)


def object_exists(object_key):
    """True / False, or None if the store can't be asked (then callers must not assume gone)."""
    from botocore.exceptions import ClientError
    try:
        client().head_object(Bucket=BUCKET, Key=object_key)
        return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code"))
        return False if code in ("404", "NoSuchKey", "NotFound") else None
    except Exception:
        return None


def delete_objects(keys):
    """Deletes objects by key. Failures are printed, never raised."""
    keys = list(keys)
    if not keys:
        return
    c = client()
    for key in keys:
        try:
            c.delete_object(Bucket=BUCKET, Key=key)
        except Exception as e:
            print(f"    ! could not delete storage object {key}: {e}")


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
        print("Media storage is not configured. Add MEDIA_S3_* (Yandex) or R2_* to .env.")
    else:
        print(f"Media storage: {PROVIDER} | bucket {BUCKET} | public base {PUBLIC_BASE_URL}")
