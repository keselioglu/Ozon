"""
Daily, rating-driven content improvement job (task #43).

Picks the lowest-rated (article, color) groups from Ozon's content rating,
builds the content they are missing, and pushes it as ONE merged
/v3/product/import item per offer_id, one item at a time, each verified by
re-reading the product.

  1. Loads content_rating_snapshot.json. If it is missing or not from today,
     runs content_rating.py first (subprocess, read-only).
  2. Groups offer_ids by (article, color) (offer_ids are REBUILT from
     products.csv via content_media.rebuild_offer_id, never parsed; only
     MS- offer_ids are in scope). Archived offers are dropped. Each group is
     scored by its lowest rating, then by its missing points (sum of the
     unmet conditions' cost). Groups with real stock
     (margin_pricing.get_real_stock) come first; lowest rating first inside
     each tier. Groups improved (or found with nothing to do) in the last
     CONTENT_COOLDOWN_DAYS days are skipped (content_improvement_log.jsonl).
  3. Unmet conditions -> actions, per offer_id:
       photo-count condition unmet   -> zoom crops up to the threshold in the
                                        condition text (default
                                        TARGET_PHOTO_COUNT, capped at
                                        MAX_PHOTO_COUNT)
       video link (21841) empty      -> slideshow video + title (21837)
       rich content (11254) empty    -> content_media.build_rich_content
       size table (13164) empty and
       the category has a chart      -> content_media.size_table_for
       improve_attributes asked      -> content_attributes.suggest_attributes
  4. One merged import item per offer_id, pushed and verified one at a
     time. Each offer_id's result (actions, before rating, verified flag,
     errors) is appended to content_improvement_log.jsonl. A group stops on
     its first failed push.
  5. When every offer_id of a group verified, the group's generated files and
     their R2 copies are deleted (business instruction 2026-08-27). A group
     with a failed offer keeps its files and is logged "retry".
  6. Limits: --limit-groups (DAILY_CONTENT_GROUPS), never more than
     CONTENT_UPDATE_SHARE of the remaining /v4/product/info/limit
     daily_update (re-checked before each group, so upload_to_ozon.py and
     the 5am/10:15 runs keep their headroom), and --max-minutes.
  7. --dry-run: builds the items (placeholder media URLs, no crops/video
     rendered) and prints the planned actions; no R2 upload, no import, no
     log written.
  8. Prints the rating change of yesterday's verified batch, measured
     against today's snapshot.

Idempotent: actions are always derived from the LIVE record, so a re-run
after a crash only redoes what is still missing.

Final line:
  Done. G groups / O offers improved (photos P, videos V, rich R, size-table S, attrs A), F failed, avg rating X -> Y (yesterday's batch).
"""
import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import content_media as cm

if __name__ == "__main__" and sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SNAPSHOT_FILE = "content_rating_snapshot.json"
LOG_FILE = "content_improvement_log.jsonl"
PRODUCTS_CSV = "products.csv"

CONTENT_COOLDOWN_DAYS = 7
MEDIA_KEEP_DAYS = 3  # generated photos/videos stay on R2 this long so Ozon can fetch them
TARGET_PHOTO_COUNT = 8
MAX_PHOTO_COUNT = 15
DAILY_CONTENT_GROUPS = 150
CONTENT_UPDATE_SHARE = 0.5
DEFAULT_MAX_MINUTES = 50

ACTION_KEYS = ("photos", "video", "rich", "size_table", "attrs")

# Group statuses that start the cooldown.
COOLDOWN_STATUSES = ("verified", "nothing_to_do")

_PHOTO_RE = re.compile(r"image|photo|picture|фото|изображ|снимк|картин", re.IGNORECASE)
_VIDEO_RE = re.compile(r"video|видео", re.IGNORECASE)
_INT_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Snapshot / log files
# ---------------------------------------------------------------------------

def snapshot_is_fresh(path=SNAPSHOT_FILE, today=None):
    """True if the snapshot file exists and was written today."""
    today = today or date.today()
    if not os.path.exists(path):
        return False
    return date.fromtimestamp(os.path.getmtime(path)) == today


def ensure_snapshot(path=SNAPSHOT_FILE):
    """Runs content_rating.py (read-only) when the snapshot is missing or
    stale, then loads it. Returns {} if it still can't be read."""
    if not snapshot_is_fresh(path):
        print(f"{path} missing or not from today -- running content_rating.py first...")
        try:
            subprocess.run([sys.executable, "content_rating.py"], timeout=3600, check=False)
        except Exception as e:
            print(f"  ! content_rating.py failed: {e}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError) as e:
        print(f"  ! could not read {path}: {e}")
        return {}


def load_log(path=LOG_FILE):
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return entries


def append_log(entry, path=LOG_FILE):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def cooldown_groups(log_entries, today=None, days=CONTENT_COOLDOWN_DAYS):
    """Group keys whose LATEST group record, within the last `days` days,
    is verified / nothing_to_do. A later "retry" lifts the cooldown."""
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    latest = {}
    for e in log_entries:
        if e.get("type") != "group" or not e.get("group") or e.get("dry_run"):
            continue
        latest[e["group"]] = e  # file order == time order
    out = set()
    for key, e in latest.items():
        try:
            d = date.fromisoformat(e.get("date", ""))
        except ValueError:
            continue
        if d > cutoff and e.get("status") in COOLDOWN_STATUSES:
            out.add(key)
    return out


# ---------------------------------------------------------------------------
# Rating helpers
# ---------------------------------------------------------------------------

def missing_points(rating_record):
    """Sum of the cost of every unmet condition."""
    total = 0.0
    for g in (rating_record or {}).get("groups") or []:
        for c in g.get("unmet_conditions") or []:
            try:
                total += float(c.get("cost") or 0)
            except (TypeError, ValueError):
                continue
    return total


def _rating(rec):
    r = (rec or {}).get("rating")
    return float(r) if isinstance(r, (int, float)) else None


def photo_target_from_conditions(rating_record, default=TARGET_PHOTO_COUNT, cap=MAX_PHOTO_COUNT):
    """Photo count to aim for if a photo-count condition is unmet, else None.
    Ozon lists photo conditions as TIERS ("1 to 2 images", "3 to 4", "5 to 7", "8+") and leaves
    several unmet at once, so the target is the HIGHEST tier's lower bound (8+ -> 8), capped at
    `cap`. (Taking the first unmet tier aimed at 2 and skipped 4-photo products -- 2026-11-25.)"""
    best = None
    for g in (rating_record or {}).get("groups") or []:
        for c in g.get("unmet_conditions") or []:
            text = f"{c.get('key') or ''} {c.get('description') or ''}"
            if not _PHOTO_RE.search(text) or _VIDEO_RE.search(text):
                continue
            target = default
            for source in (c.get("key") or "", c.get("description") or ""):
                nums = [int(n) for n in _INT_RE.findall(source) if int(n) >= 1]
                if nums:
                    target = nums[0]  # tier's lower bound: "5_7" -> 5, "8+" -> 8
                    break
            best = target if best is None else max(best, target)
    return None if best is None else max(1, min(int(best), int(cap)))


_R2_MISSING_CACHE = {}


def r2_object_missing(url):
    """True if `url` is a generated-media link Ozon can't use: on Cloudflare's *.r2.dev address
    (unreachable from Russia, 2026-11-25) or one of our storage files that no longer exists.
    Any other URL, or the store can't be asked -> False (don't redo)."""
    if not isinstance(url, str):
        return False
    if ".r2.dev/" in url:
        return True
    if url in _R2_MISSING_CACHE:
        return _R2_MISSING_CACHE[url]
    try:
        import r2_storage
        key = r2_storage.key_for_url(url)
        missing = bool(key) and r2_storage.object_exists(key) is False
    except Exception:
        missing = False
    _R2_MISSING_CACHE[url] = missing
    return missing


def media_host_usable():
    """Generated photos/videos only help if Ozon can download them: a configured store that is
    not on *.r2.dev (unreachable from Russia, 2026-11-25)."""
    try:
        import r2_storage
        return r2_storage.is_configured() and ".r2.dev" not in (r2_storage.PUBLIC_BASE_URL or "")
    except Exception:
        return False


def select_actions(rating_record, record, size_table_available, photo_default=TARGET_PHOTO_COUNT,
                   photo_cap=MAX_PHOTO_COUNT, media_ok=True):
    """Planned actions for one offer_id from its rating and live record:
    {"photos": target, "video": True, "rich": True, "size_table": True,
     "attrs": [attr_id, ...]} -- only the keys that apply. media_ok False (no host Ozon can
    download from) -> no photo/video actions."""
    from content_attributes import improve_attribute_ids_from_rating

    actions = {}
    target = photo_target_from_conditions(rating_record, photo_default, photo_cap)
    if media_ok and target and len((record or {}).get("images") or []) < target:
        actions["photos"] = target
    video = cm.attr_value(record or {}, cm.ATTR_VIDEO_LINK)
    if media_ok and (not video or r2_object_missing(video)):
        actions["video"] = True
    if not cm.attr_value(record or {}, cm.ATTR_RICH_CONTENT):
        actions["rich"] = True
    if size_table_available and not cm.attr_value(record or {}, cm.ATTR_SIZE_TABLE):
        actions["size_table"] = True
    attr_ids = improve_attribute_ids_from_rating(rating_record)
    if attr_ids:
        actions["attrs"] = attr_ids
    return actions


def describe_actions(actions, record=None):
    parts = []
    if "photos" in actions:
        have = len((record or {}).get("images") or [])
        parts.append(f"photos {have}->{actions['photos']}")
    for key, label in (("video", "video"), ("rich", "rich"), ("size_table", "size-table")):
        if key in actions:
            parts.append(label)
    if "attrs" in actions:
        attrs = actions["attrs"]
        ids = sorted(attrs) if isinstance(attrs, dict) else attrs
        parts.append(f"attrs {list(ids)}")
    return ", ".join(parts) or "nothing"


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def load_products(path=PRODUCTS_CSV):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"  ! {path} not found")
        return []


def build_offer_index(rows):
    """offer_id -> (article, color) and (article, color) -> group info, from
    products.csv rows. offer_ids are rebuilt (MS- only), never parsed."""
    offer_to_group = {}
    groups = {}
    for row in rows:
        article = (row.get("ms_article_code") or "").strip()
        if not article:
            continue
        color = row.get("color") or ""
        try:
            oid = cm.rebuild_offer_id(article, color, row.get("size_label"))
        except ValueError:
            oid = None
        key = cm.folder_key_for(article, color)
        info = groups.setdefault(key, {
            "key": key, "article": article, "color": color,
            "name": row.get("name") or "", "url": row.get("url") or "", "image_urls": [],
        })
        if not info["image_urls"]:
            info["image_urls"] = cm.split_image_urls(row.get("image_urls"))
        if oid and oid not in offer_to_group:
            offer_to_group[oid] = key
    return offer_to_group, groups


def build_groups(snapshot, offer_to_group, group_info, live_offer_ids=None):
    """Scored groups from the rating snapshot. Non-MS offer_ids, offer_ids
    not rebuildable from products.csv, and (if live_offer_ids is given)
    archived / no-longer-live offer_ids are left out."""
    live = set(live_offer_ids) if live_offer_ids is not None else None
    groups = {}
    for oid, rec in (snapshot or {}).items():
        if not cm.is_ms_offer_id(oid):
            continue
        if live is not None and oid not in live:
            continue
        key = offer_to_group.get(oid)
        if not key:
            continue
        g = groups.get(key)
        if g is None:
            g = dict(group_info[key])
            g.update({"offer_ids": [], "min_rating": None, "missing_points": 0.0})
            groups[key] = g
        g["offer_ids"].append(oid)
        r = _rating(rec)
        if r is not None and (g["min_rating"] is None or r < g["min_rating"]):
            g["min_rating"] = r
        g["missing_points"] = max(g["missing_points"], missing_points(rec))
    for g in groups.values():
        g["offer_ids"].sort()
    return list(groups.values())


def rank_groups(groups, stock=None, skip_keys=()):
    """In-stock groups first, then lowest rating, then most missing points.
    Groups in skip_keys (cooldown) are dropped. With stock=None (unknown)
    every group is treated alike."""
    skip_keys = set(skip_keys)
    out = []
    for g in groups:
        if g["key"] in skip_keys:
            continue
        in_stock = True if stock is None else any((stock.get(o) or 0) > 0 for o in g["offer_ids"])
        g["in_stock"] = in_stock
        out.append(g)
    out.sort(key=lambda g: (
        0 if g["in_stock"] else 1,
        g["min_rating"] if g["min_rating"] is not None else 101.0,
        -g["missing_points"],
        g["key"],
    ))
    return out


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

def allowed_updates(limit_response, share=CONTENT_UPDATE_SHARE):
    """How many daily_update slots this job may use: floor(share x what
    remains). 0 when the response is missing/unreadable or exhausted."""
    du = (limit_response or {}).get("daily_update") or {}
    limit, usage = du.get("limit"), du.get("usage")
    if not isinstance(limit, (int, float)) or not isinstance(usage, (int, float)):
        return 0
    remaining = max(0, int(limit) - int(usage))
    return max(0, int(math.floor(remaining * float(share))))


def read_update_budget(api_call, share=CONTENT_UPDATE_SHARE):
    try:
        return allowed_updates(api_call("/v4/product/info/limit", {}), share)
    except Exception as e:
        print(f"  ! could not read /v4/product/info/limit: {e} -- no pushes")
        return 0


class UpdateBudget:
    """Caps pushes at the share computed at start, re-checked live before
    each group (the live usage lags, so the smaller of the two wins)."""

    def __init__(self, api_call, share=CONTENT_UPDATE_SHARE):
        self.api_call = api_call
        self.share = share
        self.start_allowed = read_update_budget(api_call, share)
        self.used = 0

    def available(self):
        live = read_update_budget(self.api_call, self.share)
        return max(0, min(self.start_allowed - self.used, live))

    def spend(self, n=1):
        self.used += n


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def new_stats():
    return {"groups": 0, "offers": 0, "failed": 0, **{k: 0 for k in ACTION_KEYS}}


def _fmt_avg(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "n/a"


def format_done_line(stats, before_avg=None, after_avg=None):
    return (
        f"Done. {stats['groups']} groups / {stats['offers']} offers improved "
        f"(photos {stats['photos']}, videos {stats['video']}, rich {stats['rich']}, "
        f"size-table {stats['size_table']}, attrs {stats['attrs']}), "
        f"{stats['failed']} failed, avg rating {_fmt_avg(before_avg)} -> {_fmt_avg(after_avg)} "
        f"(yesterday's batch)."
    )


def yesterday_batch_change(log_entries, snapshot, today=None):
    """(count, avg before, avg after) for offer_ids verified yesterday,
    before = rating logged at push time, after = today's snapshot."""
    today = today or date.today()
    yday = (today - timedelta(days=1)).isoformat()
    pairs = {}
    for e in log_entries:
        if e.get("type") != "offer" or e.get("date") != yday or not e.get("verified") or e.get("dry_run"):
            continue
        before = e.get("before_rating")
        after = _rating((snapshot or {}).get(e.get("offer_id")))
        if isinstance(before, (int, float)) and after is not None:
            pairs[e["offer_id"]] = (float(before), after)
    if not pairs:
        return 0, None, None
    n = len(pairs)
    return n, sum(b for b, _ in pairs.values()) / n, sum(a for _, a in pairs.values()) / n


# ---------------------------------------------------------------------------
# Group processing
# ---------------------------------------------------------------------------

def fetch_stock(offer_ids):
    from margin_pricing import get_real_stock
    return get_real_stock(offer_ids)


def _load_translation(article):
    from content_attributes import load_translation
    return load_translation(article)


def _suggest_attributes(article, color, record, attr_ids, api_call):
    from content_attributes import suggest_attributes
    return suggest_attributes(article, color, record, attr_ids, api_call=api_call)


def _checks_for(applied, item):
    checks = []
    if "photos" in applied:
        checks.append(cm.check_images_at_least(len(item["images"])))
    if "video" in applied:
        checks.append(cm.check_attr_present(cm.ATTR_VIDEO_LINK))
    if "rich" in applied:
        checks.append(cm.check_attr_present(cm.ATTR_RICH_CONTENT))
    if "size_table" in applied:
        checks.append(cm.check_attr_present(cm.ATTR_SIZE_TABLE))
    for attr_id in applied.get("attrs") or {}:
        checks.append(cm.check_attr_present(int(attr_id)))
    return checks


class GroupMedia:
    """Builds each group-level asset once (lazily) and remembers what was
    uploaded so it can be cleaned up."""

    def __init__(self, group, dry_run, notes):
        self.group = group
        self.dry_run = dry_run
        self.notes = notes
        self.generated_any = False
        self._crops = None
        self._video = False
        self._rich = False
        self._size = False

    def _source_images(self, record):
        return self.group.get("image_urls") or list((record or {}).get("images") or [])

    def crop_urls(self, needed, record):
        """Up to `needed` public crop URLs (fewer if the group lacks distinct
        real photos)."""
        if self._crops is None or len(self._crops) < needed:
            key = self.group["key"]
            if self.dry_run:
                self._crops = [f"dry-run://extra_photos/{key}/zoom_{i}.jpg" for i in range(1, needed + 1)]
            else:
                src = self._source_images(record)
                try:
                    paths = cm.generate_crops(self.group["article"], self.group["color"], src,
                                              target=len(src) + needed)
                    self.generated_any = True
                    self._crops = [cm.upload_to_r2(p) for p in paths]
                except Exception as e:
                    self.notes.append(f"crops failed: {e}")
                    self._crops = []
        return self._crops[:needed]

    def video_url(self, record):
        if self._video is False:
            key = self.group["key"]
            if self.dry_run:
                self._video = f"dry-run://videos/{key}/video.mp4"
            else:
                try:
                    path = cm.generate_video(self.group["article"], self.group["color"],
                                             self._source_images(record))
                    self.generated_any = True
                    self._video = cm.upload_to_r2(path) if path else None
                    if not path:
                        self.notes.append("video: nothing rendered")
                except Exception as e:
                    self.notes.append(f"video failed: {e}")
                    self._video = None
        return self._video

    def rich_content(self, record):
        if self._rich is False:
            translation = _load_translation(self.group["article"])
            if not translation:
                self.notes.append("rich: no translation on file")
                self._rich = None
            else:
                try:
                    self._rich = cm.build_rich_content(self.group["article"], translation,
                                                       self._source_images(record))
                except Exception as e:
                    self.notes.append(f"rich failed: {e}")
                    self._rich = None
        return self._rich

    def size_table(self):
        if self._size is False:
            try:
                self._size = cm.size_table_for(self.group.get("name"), url=self.group.get("url") or None)
            except Exception as e:
                self.notes.append(f"size-table failed: {e}")
                self._size = None
        return self._size

    def cleanup(self):
        """Deletes the group's generated files locally and on R2."""
        if self.dry_run or not self.generated_any:
            return
        a, c = self.group["article"], self.group["color"]
        cm.cleanup_group_media(a, c, cm.PHOTOS_DIR, cm.R2_PREFIX_BY_LOCAL_DIR[cm.PHOTOS_DIR])
        cm.cleanup_group_media(a, c, cm.VIDEOS_DIR, cm.R2_PREFIX_BY_LOCAL_DIR[cm.VIDEOS_DIR])


def plan_group(group, snapshot, records, photo_default=TARGET_PHOTO_COUNT, photo_cap=MAX_PHOTO_COUNT):
    """[(offer_id, actions)] for offer_ids of the group that need anything.
    Size-table availability is resolved by the caller via `group['_size']`."""
    planned = []
    for oid in group["offer_ids"]:
        if not cm.is_ms_offer_id(oid):
            continue
        record = records.get(oid)
        if not record:
            continue
        actions = select_actions(snapshot.get(oid), record, group.get("_size_available", False),
                                 photo_default, photo_cap, group.get("_media_ok", True))
        if actions:
            planned.append((oid, actions))
    return planned


def build_offer_item(oid, actions, record, price_info, media, group, api_call):
    """(item, applied_actions) for one offer_id; applied omits actions whose
    content couldn't be built. item is None when nothing is left."""
    overrides = {}
    generated = []
    applied = {}
    if "photos" in actions:
        needed = actions["photos"] - len(record.get("images") or [])
        urls = media.crop_urls(needed, record) if needed > 0 else []
        if urls:
            generated = urls
            applied["photos"] = actions["photos"]
    if actions.get("video"):
        url = media.video_url(record)
        if url:
            overrides[cm.ATTR_VIDEO_LINK] = url
            overrides[cm.ATTR_VIDEO_TITLE] = record.get("name") or ""
            overrides[cm.ATTR_VIDEO_COVER] = None  # stays unset (business decision)
            applied["video"] = True
    if actions.get("rich"):
        rich = media.rich_content(record)
        if rich:
            overrides[cm.ATTR_RICH_CONTENT] = rich
            applied["rich"] = True
    if actions.get("size_table"):
        table = media.size_table()
        if table:
            overrides[cm.ATTR_SIZE_TABLE] = table
            applied["size_table"] = True
    if actions.get("attrs"):
        try:
            suggested = _suggest_attributes(group["article"], group["color"], record,
                                            actions["attrs"], api_call) or {}
        except Exception as e:
            media.notes.append(f"attrs failed: {e}")
            suggested = {}
        suggested = {int(k): v for k, v in suggested.items() if v}
        if suggested:
            for k, v in suggested.items():
                overrides.setdefault(k, v)
            applied["attrs"] = suggested
    if not applied:
        return None, {}
    item = cm.build_merged_import_item(record, price_info, attr_overrides=overrides,
                                       generated_images=generated)
    return item, applied


def process_group(group, ctx):
    """Plans, builds, pushes and verifies one group. Returns the group status:
    "verified", "retry", "nothing_to_do", "no_quota" or "dry_run"."""
    api_call, dry_run = ctx["api_call"], ctx["dry_run"]
    snapshot, stats = ctx["snapshot"], ctx["stats"]
    today = ctx["today"]

    offer_ids = [o for o in group["offer_ids"] if cm.is_ms_offer_id(o)]
    records = cm.fetch_attributes(offer_ids, api_call=api_call)
    notes = []
    media = GroupMedia(group, dry_run, notes)
    group["_size_available"] = bool(media.size_table())
    group["_media_ok"] = ctx.get("media_ok", True)
    planned = plan_group(group, snapshot, records, ctx["photo_default"], ctx["photo_cap"])

    def log_group(status, extra=None):
        if dry_run:
            return
        entry = {"type": "group", "date": today.isoformat(), "ts": datetime.now().isoformat(timespec="seconds"),
                 "group": group["key"], "article": group["article"], "color": group["color"],
                 "offers": len(planned), "status": status, "notes": notes}
        entry.update(extra or {})
        append_log(entry, ctx["log_path"])

    if not planned:
        log_group("nothing_to_do")
        return "nothing_to_do"

    budget = ctx["budget"]
    available = budget.available()
    if available < len(planned):
        print(f"  {group['key']}: needs {len(planned)} update(s), only {available} allowed -- skipped")
        return "no_quota"

    prices = cm.fetch_prices([o for o, _ in planned], api_call=api_call)
    print(f"{group['key']} (min rating {_fmt_avg(group['min_rating'])}, "
          f"{len(planned)} offer(s)){' [DRY RUN]' if dry_run else ''}")

    all_ok = True
    pushed_ok = 0
    for oid, actions in planned:
        record = records[oid]
        before = _rating(snapshot.get(oid))
        entry = {"type": "offer", "date": today.isoformat(), "ts": None, "group": group["key"],
                 "offer_id": oid, "before_rating": before, "actions": [], "verified": False,
                 "errors": None}
        try:
            item, applied = build_offer_item(oid, actions, record, prices.get(oid), media, group, api_call)
        except Exception as e:
            item, applied = None, {}
            entry["errors"] = [str(e)]
        entry["actions"] = [k for k in ACTION_KEYS if k in applied]
        if item is None:
            print(f"  {oid}: nothing buildable ({describe_actions(actions, record)})"
                  + (f" -- {entry['errors'][0]}" if entry["errors"] else ""))
            if entry["errors"]:
                all_ok = False
                stats["failed"] += 1
                entry["ts"] = datetime.now().isoformat(timespec="seconds")
                if not dry_run:
                    append_log(entry, ctx["log_path"])
                break
            continue

        if dry_run:
            print(f"  {oid}: {describe_actions(applied, record)} "
                  f"({len(item['images'])} images, {len(item['attributes'])} attributes)")
            pushed_ok += 1
            _count(stats, applied)
            continue

        budget.spend(1)
        try:
            result = cm.push_one_and_verify(item, _checks_for(applied, item), api_call=api_call)
            entry["verified"] = bool(result.get("verified"))
            entry["status"] = result.get("status")
            entry["errors"] = result.get("errors") or None
        except Exception as e:
            entry["errors"] = [str(e)]
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        append_log(entry, ctx["log_path"])
        mark = "OK " if entry["verified"] else "FAIL"
        print(f"  {mark} {oid}: {describe_actions(applied, record)}"
              + ("" if entry["verified"] else f" -- {entry['errors']}"))
        if entry["verified"]:
            pushed_ok += 1
            _count(stats, applied)
        else:
            stats["failed"] += 1
            all_ok = False
            break  # stop the group on the first rejection

    if dry_run:
        if pushed_ok:
            stats["groups"] += 1
        return "dry_run"

    if all_ok and pushed_ok:
        # Ozon downloads photos/videos from our URLs LATER, on its own schedule: deleting right
        # after the push left Ozon nothing to fetch (2026-11-25). Files stay on R2 for
        # MEDIA_KEEP_DAYS and are removed by cleanup_old_media() on a later run.
        stats["groups"] += 1
        log_group("verified", {"verified_offers": pushed_ok, "media_pending_cleanup": media.generated_any})
        return "verified"
    if all_ok:
        log_group("nothing_to_do")
        return "nothing_to_do"
    log_group("retry", {"verified_offers": pushed_ok})
    return "retry"


def _count(stats, applied):
    stats["offers"] += 1
    for k in ACTION_KEYS:
        if k in applied:
            stats[k] += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def media_due_for_cleanup(log_entries, today, keep_days=MEDIA_KEEP_DAYS):
    """[(group, article, color)] whose generated media were pushed >= keep_days ago and not yet
    cleaned up (a later 'cleanup' log entry for the group, or a newer push, resets it)."""
    pending, done = {}, set()
    for e in log_entries:
        if e.get("type") == "group" and e.get("media_pending_cleanup"):
            pending[e["group"]] = e
        elif e.get("type") == "cleanup":
            done.add((e.get("group"), e.get("pushed_date")))
    due = []
    for g, e in pending.items():
        try:
            d = date.fromisoformat(e.get("date"))
        except (TypeError, ValueError):
            continue
        if (today - d).days >= keep_days and (g, e.get("date")) not in done:
            due.append((g, e.get("article"), e.get("color"), e.get("date")))
    return due


def cleanup_old_media(log_entries, today, log_path, keep_days=MEDIA_KEEP_DAYS):
    """Deletes local + R2 copies of media pushed >= keep_days ago (business instruction
    2026-08-27: don't keep videos/crops; delayed so Ozon has fetched them first)."""
    for g, article, color, pushed in media_due_for_cleanup(log_entries, today, keep_days):
        try:
            cm.cleanup_group_media(article, color, cm.PHOTOS_DIR, cm.R2_PREFIX_BY_LOCAL_DIR[cm.PHOTOS_DIR])
            cm.cleanup_group_media(article, color, cm.VIDEOS_DIR, cm.R2_PREFIX_BY_LOCAL_DIR[cm.VIDEOS_DIR])
            append_log({"type": "cleanup", "date": today.isoformat(), "group": g, "pushed_date": pushed}, log_path)
            print(f"  cleaned up media of {g} (pushed {pushed})")
        except Exception as e:
            print(f"  ! cleanup of {g} failed: {e}")


def run(args, api_call=None, today=None, products_rows=None):
    if api_call is None:
        from ozon_client import call as api_call
    today = today or date.today()
    started = time.monotonic()
    deadline = started + max(0.0, float(args.max_minutes)) * 60

    snapshot = ensure_snapshot(args.snapshot)
    log_entries = load_log(args.log)
    if not args.dry_run:
        cleanup_old_media(log_entries, today, args.log)

    n, before_avg, after_avg = yesterday_batch_change(log_entries, snapshot, today)
    if n:
        print(f"Yesterday's batch: {n} offer(s), avg rating {before_avg:.1f} -> {after_avg:.1f}")
    else:
        print("Yesterday's batch: no verified offers to compare.")

    stats = new_stats()
    if not snapshot:
        print("No rating snapshot -- nothing to do.")
        print(format_done_line(stats, before_avg, after_avg))
        return stats

    rows = products_rows if products_rows is not None else load_products(args.products)
    offer_to_group, group_info = build_offer_index(rows)

    try:
        live = cm.find_live_ms_offer_ids(api_call=api_call)
    except Exception as e:
        print(f"  ! could not list live offer_ids: {e} -- stopping")
        print(format_done_line(stats, before_avg, after_avg))
        return stats

    groups = build_groups(snapshot, offer_to_group, group_info, live)
    try:
        stock = fetch_stock(sorted({o for g in groups for o in g["offer_ids"]}))
    except Exception as e:
        print(f"  ! stock read failed ({e}) -- ranking without stock priority")
        stock = None
    skip = cooldown_groups(log_entries, today, args.cooldown_days)
    ranked = rank_groups(groups, stock, skip)
    print(f"{len(groups)} group(s) rated, {len(skip & {g['key'] for g in groups})} in cooldown, "
          f"{len(ranked)} candidate(s)")

    budget = UpdateBudget(api_call, args.update_share)
    print(f"daily_update allowance for this job: {budget.start_allowed}")

    media_ok = media_host_usable()
    if not media_ok:
        print("Photos/videos OFF: no media host Ozon can download from (set MEDIA_S3_* in .env).")
    ctx = {"media_ok": media_ok, "api_call": api_call, "dry_run": args.dry_run, "snapshot": snapshot, "stats": stats,
           "today": today, "budget": budget, "log_path": args.log,
           "photo_default": args.photo_target, "photo_cap": MAX_PHOTO_COUNT}

    attempted = 0
    for group in ranked:
        if attempted >= args.limit_groups:
            break
        if time.monotonic() >= deadline:
            print(f"Time budget ({args.max_minutes} min) reached -- stopping cleanly.")
            break
        if budget.start_allowed - budget.used <= 0:
            print("daily_update share used up -- stopping.")
            break
        try:
            status = process_group(group, ctx)
        except Exception as e:
            print(f"  ! {group['key']}: {e}")
            stats["failed"] += 1
            status = "error"
        if status in ("verified", "retry", "dry_run", "error"):
            attempted += 1

    if args.dry_run:
        print("DRY RUN -- nothing uploaded or imported; counts above are planned, not pushed.")
    print(format_done_line(stats, before_avg, after_avg))
    return stats


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Daily rating-driven content improvement (photos, "
                                             "video, rich content, size table, attributes).")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print planned actions per offer_id; no R2 upload, no import")
    ap.add_argument("--limit-groups", type=int, default=DAILY_CONTENT_GROUPS,
                    help=f"max (article, color) groups to improve (default {DAILY_CONTENT_GROUPS})")
    ap.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES,
                    help=f"wall-clock budget; stops cleanly between groups (default {DEFAULT_MAX_MINUTES})")
    ap.add_argument("--cooldown-days", type=int, default=CONTENT_COOLDOWN_DAYS,
                    help=f"skip groups improved within this many days (default {CONTENT_COOLDOWN_DAYS})")
    ap.add_argument("--photo-target", type=int, default=TARGET_PHOTO_COUNT,
                    help=f"photo count when the condition gives none (default {TARGET_PHOTO_COUNT}, "
                         f"max {MAX_PHOTO_COUNT})")
    ap.add_argument("--update-share", type=float, default=CONTENT_UPDATE_SHARE,
                    help=f"max share of the remaining daily_update quota (default {CONTENT_UPDATE_SHARE})")
    ap.add_argument("--snapshot", default=SNAPSHOT_FILE, help=argparse.SUPPRESS)
    ap.add_argument("--log", default=LOG_FILE, help=argparse.SUPPRESS)
    ap.add_argument("--products", default=PRODUCTS_CSV, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.photo_target = max(1, min(args.photo_target, MAX_PHOTO_COUNT))
    args.update_share = max(0.0, min(args.update_share, CONTENT_UPDATE_SHARE))
    return args


def main(argv=None):
    run(parse_args(argv))


if __name__ == "__main__":
    main()
