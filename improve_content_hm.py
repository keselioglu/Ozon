"""
Daily content improvement for H&M listings (HM-<10-digit article>-<size>), the H&M twin of
improve_content.py (business instruction 2026-11-25: "M&S and H&M together").

Per H&M article (= product + colour), lowest Ozon content rating first, in-stock first:
  photos        H&M's OWN real photos (www2.hm.com product page, image.hm.com) when H&M has
                more than Ozon shows -- no generated crops, no hosting needed (Ozon fetched
                image.hm.com fine on 2026-11-25)
  video         slideshow of the real photos -- only when a media host Ozon can download from
                is configured (improve_content.media_host_usable: MEDIA_S3_* / Yandex)
  rich content  content_media.build_rich_content from the translation + first H&M photo
  attributes    content_attributes.suggest_attributes with the H&M page as the source
  description   content_text.build_long_annotation (600-1500 chars, facts only)
No size table (the size charts are M&S's).

Same safety rails as improve_content.py: one merged import per offer, pushed and re-read one at
a time, a group stops at its first failed push, shared content_improvement_log.jsonl (keys
HM_<article>), 7-day cooldown, CONTENT_UPDATE_SHARE of the remaining daily_update quota, time
budget, --dry-run (nothing pushed or written).

The translation for an article is the auto_translate entry if one exists (products added by
run_new_products.py), else it is read back from the live Ozon record (name, annotation,
composition, care) -- text that is already on the listing.

Reads H&M through hm_site (the H&M Chrome profile) -- not while the 02:00 H&M stock job runs.
"""
import argparse
import json
import sys
import time
from datetime import date, datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import content_media as cm
import improve_content as ic

SNAPSHOT_FILE = "content_rating_snapshot_hm.json"
MAX_PHOTOS = 15
ATTR_COMPOSITION, ATTR_CARE = 4604, 4655


def hm_snapshot(path=SNAPSHOT_FILE, today=None):
    """{offer_id: rating record} for live HM- offers -- today's file, or a fresh read-only read."""
    import content_rating as cr
    today = (today or date.today()).isoformat()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("_date") == today:
            return {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, ValueError):
        pass
    products = cr.fetch_live_products("HM-")
    snap = cr.fetch_ratings(cr.fetch_skus(products.values()))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_date": today, **snap}, f, ensure_ascii=False)
    print(f"H&M content rating: {len(snap)} offer(s) rated -> {path}")
    return snap


def build_groups(snapshot):
    groups = {}
    for oid, rec in snapshot.items():
        if not cm.is_hm_offer_id(oid):
            continue
        art = oid.split("-")[1]
        g = groups.setdefault(art, {"key": f"HM_{art}", "article": art, "color": "", "offer_ids": [],
                                    "min_rating": None})
        g["offer_ids"].append(oid)
        r = rec.get("rating")
        if isinstance(r, (int, float)):
            g["min_rating"] = r if g["min_rating"] is None else min(g["min_rating"], r)
    return list(groups.values())


def rank(groups, stock, skip):
    def key(g):
        in_stock = any((stock or {}).get(o, 0) > 0 for o in g["offer_ids"]) if stock is not None else True
        return (0 if in_stock else 1, g["min_rating"] if g["min_rating"] is not None else 999)
    return sorted((g for g in groups if g["key"] not in skip), key=key)


def translation_for(article, record):
    from ozon_translations import PRODUCT_TRANSLATIONS
    tr = PRODUCT_TRANSLATIONS.get(article)
    if tr:
        return tr
    name = (record.get("name") or "")
    name = name[4:] if name.startswith("H&M ") else name
    desc = cm.attr_value(record, ic.ATTR_ANNOTATION) or ""
    if not name or not desc:
        return None
    comp = cm.attr_value(record, ATTR_COMPOSITION) or ""
    return {"name": name, "description": desc, "material_text": comp, "material_composition": comp,
            "care_text": cm.attr_value(record, ATTR_CARE) or ""}


def hm_source(detail):
    v = (detail or {}).get("variation") or {}
    return {"name": (detail or {}).get("details", {}).get("productName"), "description": v.get("description"),
            "specs": {"composition": v.get("compositions"), "care": v.get("careInstructions"), "colour": v.get("name")}}


def plan_offer(rating, record, hm_images, media_ok):
    """Actions for one H&M offer -- improve_content.select_actions, with photos meaning
    'more real H&M photos' and no size table."""
    actions = ic.select_actions(rating, record, False, media_ok=media_ok)
    actions.pop("photos", None)
    have = len(record.get("images") or [])
    target = ic.photo_target_from_conditions(rating)
    if target and have < target and len(hm_images) > have:
        actions["photos"] = min(len(hm_images), MAX_PHOTOS)
    return actions


def process_group(group, hm, ctx):
    api_call, dry_run, stats, today = ctx["api_call"], ctx["dry_run"], ctx["stats"], ctx["today"]
    records = cm.fetch_attributes(group["offer_ids"], api_call=api_call)
    detail = hm.product(group["article"])
    notes = []
    if not detail:
        notes.append("H&M page gone")
        return "nothing_to_do"
    hm_images = [u for u in __import__("hm_catalog").images_for(detail, limit=MAX_PHOTOS)]
    planned = []
    for oid in group["offer_ids"]:
        rec = records.get(oid)
        if rec:
            acts = plan_offer(ctx["snapshot"].get(oid), rec, hm_images, ctx["media_ok"])
            if acts:
                planned.append((oid, acts))
    if not planned:
        if not dry_run:
            ic.append_log({"type": "group", "date": today.isoformat(), "ts": datetime.now().isoformat(timespec="seconds"),
                           "group": group["key"], "article": group["article"], "color": "", "brand": "H&M",
                           "offers": 0, "status": "nothing_to_do", "notes": notes}, ctx["log_path"])
        return "nothing_to_do"
    if ctx["budget"].available() < len(planned):
        return "no_quota"

    first = records[planned[0][0]]
    tr = translation_for(group["article"], first)
    src = hm_source(detail)
    cache = {}

    def rich():
        if "rich" not in cache:
            cache["rich"] = cm.build_rich_content(group["article"], tr, hm_images) if tr else None
        return cache["rich"]

    def annotation():
        if "ann" not in cache:
            if dry_run:
                cache["ann"] = "Проверка " * 70
            elif tr:
                from content_text import build_long_annotation
                text, err = build_long_annotation(first.get("name"), tr, src)
                if err:
                    notes.append(f"annotation: {err}")
                cache["ann"] = text
            else:
                cache["ann"] = None
        return cache["ann"]

    def video():
        if "video" not in cache:
            if dry_run:
                cache["video"] = f"dry-run://videos/{group['key']}/video.mp4"
            else:
                try:
                    path = cm.generate_video(group["article"], "HM", hm_images)
                    cache["video"] = cm.upload_to_r2(path) if path else None
                except Exception as e:
                    notes.append(f"video failed: {e}")
                    cache["video"] = None
        return cache["video"]

    prices = cm.fetch_prices([o for o, _ in planned], api_call=api_call)
    print(f"{group['key']} (min rating {ic._fmt_avg(group['min_rating'])}, {len(planned)} offer(s))"
          f"{' [DRY RUN]' if dry_run else ''}")
    ok_all, pushed = True, 0
    for oid, acts in planned:
        rec = records[oid]
        overrides, applied, images = {}, {}, None
        if "photos" in acts:
            images = hm_images[:acts["photos"]]
            applied["photos"] = len(images)
        if acts.get("video") and video():
            overrides[cm.ATTR_VIDEO_LINK] = cache["video"]
            overrides[cm.ATTR_VIDEO_TITLE] = rec.get("name") or ""
            applied["video"] = True
        if acts.get("rich"):
            try:
                if rich():
                    overrides[cm.ATTR_RICH_CONTENT] = cache["rich"]
                    applied["rich"] = True
            except Exception as e:
                notes.append(f"rich failed: {e}")
        if acts.get("attrs") and tr:
            try:
                from content_attributes import suggest_attributes
                sug = suggest_attributes(group["article"], "", rec, acts["attrs"], api_call=api_call,
                                         translation=tr, source=src) or {}
                sug = {int(k): v for k, v in sug.items() if v}
                if sug:
                    for k, v in sug.items():
                        overrides.setdefault(k, v)
                    applied["attrs"] = sug
            except Exception as e:
                notes.append(f"attrs failed: {e}")
        if acts.get("annotation") and annotation():
            overrides[ic.ATTR_ANNOTATION] = cache["ann"]
            applied["annotation"] = True
        if not applied:
            continue
        item = cm.build_merged_import_item(rec, prices.get(oid), images=images, attr_overrides=overrides)
        desc = ic.describe_actions(applied if "photos" not in applied else {**applied, "photos": applied["photos"]}, rec)
        if dry_run:
            print(f"  {oid}: {desc} ({len(item['images'])} images, {len(item['attributes'])} attributes)")
            pushed += 1
            ic._count(stats, applied)
            continue
        ctx["budget"].spend(1)
        entry = {"type": "offer", "date": today.isoformat(), "group": group["key"], "offer_id": oid,
                 "before_rating": ic._rating(ctx["snapshot"].get(oid)), "actions": [k for k in ic.ACTION_KEYS if k in applied],
                 "verified": False, "errors": None}
        try:
            res = cm.push_one_and_verify(item, ic._checks_for(applied, item), api_call=api_call)
            entry.update(verified=bool(res.get("verified")), status=res.get("status"), errors=res.get("errors") or None)
        except Exception as e:
            entry["errors"] = [str(e)]
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        ic.append_log(entry, ctx["log_path"])
        print(f"  {'OK ' if entry['verified'] else 'FAIL'} {oid}: {desc}" + ("" if entry["verified"] else f" -- {entry['errors']}"))
        if entry["verified"]:
            pushed += 1
            ic._count(stats, applied)
        else:
            stats["failed"] += 1
            ok_all = False
            break
    if dry_run:
        stats["groups"] += 1 if pushed else 0
        return "dry_run"
    status = "verified" if ok_all and pushed else ("nothing_to_do" if ok_all else "retry")
    if status == "verified":
        stats["groups"] += 1
    ic.append_log({"type": "group", "date": today.isoformat(), "ts": datetime.now().isoformat(timespec="seconds"),
                   "group": group["key"], "article": group["article"], "color": "HM", "brand": "H&M",
                   "offers": len(planned), "status": status, "notes": notes, "verified_offers": pushed,
                   "media_pending_cleanup": "video" in cache and bool(cache.get("video"))}, ctx["log_path"])
    return status


def run(args, api_call=None, today=None):
    if api_call is None:
        from ozon_client import call as api_call
    today = today or date.today()
    deadline = time.monotonic() + float(args.max_minutes) * 60
    stats = ic.new_stats()
    snapshot = hm_snapshot(args.snapshot, today)
    groups = build_groups(snapshot)
    try:
        from margin_pricing import get_real_stock
        stock = get_real_stock(sorted({o for g in groups for o in g["offer_ids"]}))
    except Exception as e:
        print(f"  ! stock read failed ({e}) -- ranking without stock priority")
        stock = None
    log_entries = ic.load_log(args.log)
    skip = ic.cooldown_groups(log_entries, today, args.cooldown_days)
    ranked = rank(groups, stock, skip)
    media_ok = ic.media_host_usable()
    print(f"{len(groups)} H&M group(s) rated, {len(ranked)} candidate(s); photos from H&M; "
          f"videos {'on' if media_ok else 'OFF (no media host Ozon can download from)'}")
    budget = ic.UpdateBudget(api_call, args.update_share)
    ctx = {"api_call": api_call, "dry_run": args.dry_run, "snapshot": snapshot, "stats": stats, "today": today,
           "budget": budget, "log_path": args.log, "media_ok": media_ok}
    if not ranked:
        print(ic.format_done_line(stats))
        return stats
    from hm_site import HMBrowser
    attempted = 0
    with HMBrowser() as hm:
        for g in ranked:
            if attempted >= args.limit_groups or time.monotonic() >= deadline:
                break
            if budget.start_allowed - budget.used <= 0:
                print("daily_update share used up -- stopping.")
                break
            try:
                st = process_group(g, hm, ctx)
            except Exception as e:
                print(f"  ! {g['key']}: {type(e).__name__}: {e}")
                stats["failed"] += 1
                st = "error"
            if st in ("verified", "retry", "dry_run", "error"):
                attempted += 1
    if args.dry_run:
        print("DRY RUN -- nothing uploaded or imported.")
    print(ic.format_done_line(stats))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily content improvement for H&M listings.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit-groups", type=int, default=75)
    ap.add_argument("--max-minutes", type=float, default=40)
    ap.add_argument("--cooldown-days", type=int, default=ic.CONTENT_COOLDOWN_DAYS)
    ap.add_argument("--update-share", type=float, default=ic.CONTENT_UPDATE_SHARE)
    ap.add_argument("--snapshot", default=SNAPSHOT_FILE, help=argparse.SUPPRESS)
    ap.add_argument("--log", default=ic.LOG_FILE, help=argparse.SUPPRESS)
    run(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
