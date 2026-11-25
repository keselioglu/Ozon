"""Offline tests for improve_content.py -- Ozon, R2, ffmpeg and Claude are mocked."""
import json
import re
from datetime import date

import pytest

import content_media as cm
import improve_content as ic

TODAY = date(2026, 11, 24)


def _cond(key, description, cost=5):
    return {"key": key, "description": description, "cost": cost}


def _rating(rating, unmet=(), improve_attrs=()):
    return {
        "rating": rating,
        "groups": [
            {"key": "media", "unmet_conditions": [c for c in unmet if c["key"].startswith("media")],
             "improve_attributes": []},
            {"key": "important_attributes",
             "unmet_conditions": [c for c in unmet if not c["key"].startswith("media")],
             "improve_attributes": [{"id": a, "name": f"attr {a}"} for a in improve_attrs]},
        ],
    }


PHOTO_UNMET = _cond("media_images_count", "Добавьте не менее 10 фото", 10)


def _record(offer_id, images=3, video=None, rich=None, size=None):
    attrs = [{"id": 9163, "values": [{"dictionary_value_id": 1, "value": "Женский"}]}]
    if video:
        attrs.append({"id": cm.ATTR_VIDEO_LINK, "values": [{"value": video}]})
    if rich:
        attrs.append({"id": cm.ATTR_RICH_CONTENT, "values": [{"value": rich}]})
    if size:
        attrs.append({"id": cm.ATTR_SIZE_TABLE, "values": [{"value": size}]})
    return {
        "offer_id": offer_id, "name": "Платье", "description_category_id": 200000933,
        "type_id": 1, "attributes": attrs,
        "images": [f"https://ms/{offer_id}/{i}.jpg" for i in range(images)],
        "primary_image": None,
        "weight": 1, "weight_unit": "g", "depth": 1, "width": 1, "height": 1, "dimension_unit": "mm",
    }


# --- pure helpers ------------------------------------------------------------

def test_photo_target_reads_threshold_and_caps():
    assert ic.photo_target_from_conditions(_rating(50, [PHOTO_UNMET])) == 10
    big = _cond("media_images", "Нужно 20 изображений")
    assert ic.photo_target_from_conditions(_rating(50, [big])) == ic.MAX_PHOTO_COUNT
    plain = _cond("media_photo", "Добавьте больше фото")
    assert ic.photo_target_from_conditions(_rating(50, [plain])) == ic.TARGET_PHOTO_COUNT
    video_only = _cond("media_video", "Добавьте видео")
    assert ic.photo_target_from_conditions(_rating(50, [video_only])) is None
    assert ic.photo_target_from_conditions(_rating(50, [])) is None


def test_select_actions_photo_unmet_gives_crops_and_video_set_gives_no_video():
    rec = _record("MS-A-BLACK-40", images=3, video="https://v.mp4", rich="{}", size="{}")
    actions = ic.select_actions(_rating(40, [PHOTO_UNMET]), rec, size_table_available=True)
    assert actions == {"photos": 10}


def test_select_actions_fills_missing_video_rich_size_and_attrs():
    rec = _record("MS-A-BLACK-40", images=12)
    rating = _rating(40, [PHOTO_UNMET, _cond("imp_attr", "Заполните атрибуты")],
                     improve_attrs=[4495, 31, cm.ATTR_RICH_CONTENT])
    actions = ic.select_actions(rating, rec, size_table_available=True)
    # 12 images already >= 10 -> no crops; protected/managed attrs filtered out
    assert actions == {"video": True, "rich": True, "size_table": True, "attrs": [4495]}
    no_chart = ic.select_actions(rating, rec, size_table_available=False)
    assert "size_table" not in no_chart


def test_missing_points_sums_unmet_cost():
    rating = _rating(40, [_cond("media_a", "x", 10), _cond("imp", "y", 2.5)])
    assert ic.missing_points(rating) == 12.5


def test_allowed_updates_cap_math():
    assert ic.allowed_updates({"daily_update": {"limit": 1000, "usage": 400}}) == 300
    assert ic.allowed_updates({"daily_update": {"limit": 1000, "usage": 999}}) == 0  # floor(0.5)
    assert ic.allowed_updates({"daily_update": {"limit": 1000, "usage": 1000}}) == 0
    assert ic.allowed_updates({"daily_update": {"limit": 1000, "usage": 1200}}) == 0
    assert ic.allowed_updates({}) == 0
    assert ic.allowed_updates(None) == 0
    assert ic.allowed_updates({"daily_update": {"limit": 11, "usage": 0}}, share=0.5) == 5


def test_update_budget_takes_smaller_of_start_and_live():
    usage = {"v": 0}

    def api(path, payload=None):
        assert path == "/v4/product/info/limit"
        return {"daily_update": {"limit": 100, "usage": usage["v"]}}

    b = ic.UpdateBudget(api)
    assert b.start_allowed == 50
    b.spend(10)
    assert b.available() == 40          # live usage lags -> own counter wins
    usage["v"] = 80
    assert b.available() == 10          # live usage caught up and is smaller


def test_cooldown_skip_and_retry_lifts_it():
    log = [
        {"type": "group", "group": "A_BLACK", "date": "2026-11-20", "status": "verified"},
        {"type": "group", "group": "B_RED", "date": "2026-11-10", "status": "verified"},   # too old
        {"type": "group", "group": "C_BLUE", "date": "2026-11-22", "status": "verified"},
        {"type": "group", "group": "C_BLUE", "date": "2026-11-23", "status": "retry"},
        {"type": "group", "group": "D_GREEN", "date": "2026-11-23", "status": "nothing_to_do"},
        {"type": "group", "group": "E_WHITE", "date": "2026-11-23", "status": "verified", "dry_run": True},
    ]
    assert ic.cooldown_groups(log, TODAY, 7) == {"A_BLACK", "D_GREEN"}


def test_rank_groups_in_stock_first_then_lowest_rating():
    groups = [
        {"key": "g1", "offer_ids": ["MS-1"], "min_rating": 20.0, "missing_points": 5},
        {"key": "g2", "offer_ids": ["MS-2"], "min_rating": 40.0, "missing_points": 5},
        {"key": "g3", "offer_ids": ["MS-3"], "min_rating": 40.0, "missing_points": 30},
        {"key": "g4", "offer_ids": ["MS-4"], "min_rating": 10.0, "missing_points": 5},
    ]
    ranked = ic.rank_groups(groups, stock={"MS-1": 0, "MS-2": 3, "MS-3": 1, "MS-4": 2}, skip_keys={"g4"})
    assert [g["key"] for g in ranked] == ["g3", "g2", "g1"]


def test_build_groups_mixed_prefixes_only_ms_and_live():
    rows = [
        {"ms_article_code": "T1", "color": "Black", "size_label": "40 (UK 12)", "name": "Платье",
         "url": "u", "image_urls": "a|b"},
        {"ms_article_code": "T1", "color": "Black", "size_label": "42 (UK 14)", "name": "Платье",
         "url": "u", "image_urls": "a|b"},
        {"ms_article_code": "T1", "color": "Black", "size_label": "44 (UK 16)", "name": "Платье",
         "url": "u", "image_urls": "a|b"},
    ]
    offer_to_group, info = ic.build_offer_index(rows)
    snapshot = {
        "MS-T1-BLACK-40": _rating(30), "MS-T1-BLACK-42": _rating(50),
        "MS-T1-BLACK-44": _rating(10),            # archived -> not live
        "MAR-T1-BLACK-40": _rating(5),            # legacy prefix
    }
    offer_to_group["MAR-T1-BLACK-40"] = "T1_BLACK"  # even if it were mapped
    groups = ic.build_groups(snapshot, offer_to_group, info,
                             live_offer_ids={"MS-T1-BLACK-40", "MS-T1-BLACK-42", "MAR-T1-BLACK-40"})
    assert len(groups) == 1
    g = groups[0]
    assert g["offer_ids"] == ["MS-T1-BLACK-40", "MS-T1-BLACK-42"]
    assert g["min_rating"] == 30.0
    assert g["image_urls"] == ["a", "b"]


def test_yesterday_batch_change_and_summary_line():
    log = [
        {"type": "offer", "date": "2026-11-23", "offer_id": "MS-1", "verified": True, "before_rating": 40},
        {"type": "offer", "date": "2026-11-23", "offer_id": "MS-2", "verified": True, "before_rating": 60},
        {"type": "offer", "date": "2026-11-23", "offer_id": "MS-3", "verified": False, "before_rating": 10},
        {"type": "offer", "date": "2026-11-22", "offer_id": "MS-4", "verified": True, "before_rating": 10},
    ]
    snap = {"MS-1": {"rating": 70}, "MS-2": {"rating": 80}, "MS-3": {"rating": 99}, "MS-4": {"rating": 99}}
    n, before, after = ic.yesterday_batch_change(log, snap, TODAY)
    assert (n, before, after) == (2, 50.0, 75.0)

    stats = {"groups": 3, "offers": 7, "failed": 1, "photos": 2, "video": 3, "rich": 4,
             "size_table": 5, "attrs": 6}
    line = ic.format_done_line(stats, before, after)
    assert line == ("Done. 3 groups / 7 offers improved (photos 2, videos 3, rich 4, size-table 5, "
                    "attrs 6), 1 failed, longer descriptions 0, avg rating 50.0 -> 75.0 (yesterday's batch).")
    assert re.match(r"^Done\. \d+ groups / \d+ offers improved \(photos \d+, videos \d+, rich \d+, "
                    r"size-table \d+, attrs \d+\), \d+ failed, longer descriptions \d+, avg rating \S+ -> \S+ "
                    r"\(yesterday's batch\)\.$",
                    ic.format_done_line(ic.new_stats()))


# --- end-to-end run with mocks -----------------------------------------------

ROWS = [
    {"ms_article_code": "T1", "color": "Black", "size_label": f"{s} (UK {s - 28})", "name": "Elbise",
     "url": "https://ms/elbise", "image_urls": "https://ms/1.jpg|https://ms/2.jpg|https://ms/3.jpg"}
    for s in (40, 42)
] + [
    {"ms_article_code": "T2", "color": "Red", "size_label": "40 (UK 12)", "name": "Elbise",
     "url": "https://ms/elbise2", "image_urls": "https://ms/x.jpg"},
]


class FakeOzon:
    def __init__(self, limit=1000, usage=0):
        self.calls = []
        self.limit = limit
        self.usage = usage
        self.records = {
            "MS-T1-BLACK-40": _record("MS-T1-BLACK-40", images=3),
            "MS-T1-BLACK-42": _record("MS-T1-BLACK-42", images=3),
            "MS-T2-RED-40": _record("MS-T2-RED-40", images=1),
        }

    def __call__(self, path, payload=None):
        self.calls.append(path)
        if path == "/v4/product/info/limit":
            return {"daily_update": {"limit": self.limit, "usage": self.usage}}
        if path == "/v3/product/list":
            return {"result": {"items": [{"offer_id": o} for o in self.records], "last_id": ""}}
        if path == "/v4/product/info/attributes":
            ids = payload["filter"]["offer_id"]
            return {"result": [self.records[o] for o in ids if o in self.records]}
        if path == "/v5/product/info/prices":
            ids = payload["filter"]["offer_id"]
            return {"items": [{"offer_id": o, "price": {"price": "10", "currency_code": "USD", "vat": "0"}}
                              for o in ids]}
        raise AssertionError(f"unexpected Ozon call {path}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    snap = {
        "MS-T1-BLACK-40": _rating(30, [PHOTO_UNMET], improve_attrs=[4495]),
        "MS-T1-BLACK-42": _rating(35, [PHOTO_UNMET], improve_attrs=[4495]),
        "MS-T2-RED-40": _rating(60, [PHOTO_UNMET]),
    }
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    log_path = tmp_path / "log.jsonl"

    calls = {"crops": 0, "video": 0, "r2": [], "cleanup": [], "push": [], "delete_r2": 0}

    def fake_crops(article, color, urls, target=None, output_dir=None):
        calls["crops"] += 1
        return [f"generated_photos/{article}_{color.upper()}/zoom_{i}.jpg" for i in range(1, target - len(urls) + 1)]

    def fake_video(article, color, urls, output_dir=None):
        calls["video"] += 1
        return f"generated_videos/{article}_{color.upper()}/video.mp4"

    def fake_upload(path, key=None):
        calls["r2"].append(path)
        return "https://r2/" + cm.r2_object_key_for(path)

    monkeypatch.setattr(ic, "ensure_snapshot", lambda path: json.loads(open(path, encoding="utf-8").read()))
    monkeypatch.setattr(ic, "fetch_stock", lambda oids: {o: 5 for o in oids})
    monkeypatch.setattr(ic, "_load_translation", lambda a: {"name": "Платье"})
    monkeypatch.setattr(ic, "_suggest_attributes",
                        lambda a, c, rec, ids, api: {4495: [{"dictionary_value_id": 7, "value": "Лето"}]})
    monkeypatch.setattr(cm, "generate_crops", fake_crops)
    monkeypatch.setattr(cm, "generate_video", fake_video)
    monkeypatch.setattr(cm, "upload_to_r2", fake_upload)
    monkeypatch.setattr(cm, "build_rich_content", lambda a, t, u: '{"content": []}')
    monkeypatch.setattr(cm, "size_table_for", lambda name, url=None: '{"table": 1}')
    monkeypatch.setattr(cm, "cleanup_group_media", lambda a, c, d, p: calls["cleanup"].append((a, c, d, p)))
    monkeypatch.setattr(ic, "media_host_usable", lambda: True)  # tests don't depend on the .env host
    monkeypatch.setattr(ic, "r2_object_missing", lambda url: False)

    def no_delete(keys):
        calls["delete_r2"] += 1
    monkeypatch.setattr(cm, "delete_r2", no_delete)

    def args(*extra):
        return ic.parse_args(["--snapshot", str(snap_path), "--log", str(log_path), *extra])

    return {"args": args, "calls": calls, "log": log_path, "snap": snap}


def _push_ok(calls, fail_offer=None):
    def push(item, checks, api_call=None):
        calls["push"].append(item)
        return {"verified": item["offer_id"] != fail_offer, "status": "imported",
                "errors": [] if item["offer_id"] != fail_offer else [{"code": "BR_x"}]}
    return push


def _log(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_run_pushes_merged_item_per_offer_and_cleans_up(env, monkeypatch, capsys):
    api = FakeOzon()
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"]))
    stats = ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)

    pushes = env["calls"]["push"]
    assert [p["offer_id"] for p in pushes] == ["MS-T1-BLACK-40", "MS-T1-BLACK-42", "MS-T2-RED-40"]
    item = pushes[0]
    by_id = {a["id"]: a["values"] for a in item["attributes"]}
    assert by_id[cm.ATTR_VIDEO_LINK] == [{"value": "https://r2/videos/T1_BLACK/video.mp4"}]
    assert by_id[cm.ATTR_VIDEO_TITLE] == [{"value": "Платье"}]
    assert cm.ATTR_RICH_CONTENT in by_id and cm.ATTR_SIZE_TABLE in by_id
    assert by_id[4495] == [{"dictionary_value_id": 7, "value": "Лето"}]
    assert len(item["images"]) == 10 and item["images"][:3] == api.records["MS-T1-BLACK-40"]["images"]
    # one crop/video build per group, reused for both sizes
    assert env["calls"]["crops"] == 2 and env["calls"]["video"] == 2
    assert stats["groups"] == 2 and stats["offers"] == 3 and stats["failed"] == 0
    assert stats["photos"] == 3 and stats["attrs"] == 2
    # verified groups keep their media (Ozon fetches later); deleted MEDIA_KEEP_DAYS later
    assert env["calls"]["cleanup"] == []
    later = TODAY + __import__("datetime").timedelta(days=ic.MEDIA_KEEP_DAYS)
    ic.cleanup_old_media(_log(env["log"]), later, env["log"])
    dirs = sorted((a, d) for a, c, d, p in env["calls"]["cleanup"])
    assert dirs == [("T1", "generated_photos"), ("T1", "generated_videos"),
                    ("T2", "generated_photos"), ("T2", "generated_videos")]
    env["calls"]["cleanup"].clear()
    ic.cleanup_old_media(_log(env["log"]), later, env["log"])  # cleaned once only
    assert env["calls"]["cleanup"] == []
    log = _log(env["log"])
    offers = [e for e in log if e["type"] == "offer"]
    assert {e["offer_id"] for e in offers} == set(api.records)
    first = next(e for e in offers if e["offer_id"] == "MS-T1-BLACK-40")
    assert first["verified"] is True and first["before_rating"] == 30
    assert first["actions"] == ["photos", "video", "rich", "size_table", "attrs"]
    assert [e["status"] for e in log if e["type"] == "group"] == ["verified", "verified"]
    out = capsys.readouterr().out.strip().splitlines()
    done = [ln for ln in out if ln.startswith("Done.")]
    assert done[-1].startswith("Done. 2 groups / 3 offers improved (photos 3, videos 3, rich 3, "
                               "size-table 3, attrs 2), 0 failed")

    # re-run the same day: both groups are in cooldown -> no pushes
    env["calls"]["push"].clear()
    ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)
    assert env["calls"]["push"] == []


def test_failed_verify_keeps_files_and_marks_retry(env, monkeypatch):
    api = FakeOzon()
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"], fail_offer="MS-T1-BLACK-40"))
    stats = ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)

    # T1 stopped at its first failure; T2 still went through
    assert [p["offer_id"] for p in env["calls"]["push"]] == ["MS-T1-BLACK-40", "MS-T2-RED-40"]
    assert stats["failed"] == 1 and stats["groups"] == 1
    assert env["calls"]["cleanup"] == []   # nothing deleted on the push day
    later = TODAY + __import__("datetime").timedelta(days=ic.MEDIA_KEEP_DAYS)
    ic.cleanup_old_media(_log(env["log"]), later, env["log"])
    assert {a for a, *_ in env["calls"]["cleanup"]} == {"T2"}   # T1 (retry) files kept
    assert env["calls"]["delete_r2"] == 0
    groups = {e["group"]: e["status"] for e in _log(env["log"]) if e["type"] == "group"}
    assert groups == {"T1_BLACK": "retry", "T2_RED": "verified"}
    failed = next(e for e in _log(env["log"]) if e.get("offer_id") == "MS-T1-BLACK-40")
    assert failed["verified"] is False and failed["errors"] == [{"code": "BR_x"}]

    # next run: T1 is retried (not in cooldown), T2 is
    env["calls"]["push"].clear()
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"]))
    ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)
    assert [p["offer_id"] for p in env["calls"]["push"]] == ["MS-T1-BLACK-40", "MS-T1-BLACK-42"]


def test_quota_exhausted_means_zero_pushes(env, monkeypatch):
    api = FakeOzon(limit=1000, usage=1000)
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"]))
    stats = ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)
    assert env["calls"]["push"] == []
    assert "/v3/product/import" not in api.calls
    assert stats["offers"] == 0


def test_quota_share_never_exceeded(env, monkeypatch):
    # remaining 4 -> share 2: T1 needs 2 (fits), T2 needs 1 (no room left)
    api = FakeOzon(limit=1000, usage=996)
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"]))
    ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)
    assert len(env["calls"]["push"]) == 2

    # remaining 2 -> share 1: T1 (2 offers) doesn't fit, T2 (1 offer) does
    env["log"].unlink()
    env["calls"]["push"].clear()
    api = FakeOzon(limit=1000, usage=998)
    ic.run(env["args"](), api_call=api, today=TODAY, products_rows=ROWS)
    assert [p["offer_id"] for p in env["calls"]["push"]] == ["MS-T2-RED-40"]


def test_limit_groups(env, monkeypatch):
    api = FakeOzon()
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"]))
    ic.run(env["args"]("--limit-groups", "1"), api_call=api, today=TODAY, products_rows=ROWS)
    assert {p["offer_id"] for p in env["calls"]["push"]} == {"MS-T1-BLACK-40", "MS-T1-BLACK-42"}


def test_max_minutes_zero_stops_before_any_group(env, monkeypatch):
    api = FakeOzon()
    monkeypatch.setattr(cm, "push_one_and_verify", _push_ok(env["calls"]))
    ic.run(env["args"]("--max-minutes", "0"), api_call=api, today=TODAY, products_rows=ROWS)
    assert env["calls"]["push"] == []


def test_dry_run_makes_no_write_calls(env, monkeypatch, capsys):
    api = FakeOzon()

    def boom(*a, **k):
        raise AssertionError("write call in dry run")
    for name in ("push_one_and_verify", "upload_to_r2", "delete_r2", "cleanup_group_media",
                 "generate_crops", "generate_video"):
        monkeypatch.setattr(cm, name, boom)

    stats = ic.run(env["args"]("--dry-run"), api_call=api, today=TODAY, products_rows=ROWS)

    assert "/v3/product/import" not in api.calls
    assert set(api.calls) <= {"/v4/product/info/limit", "/v3/product/list",
                              "/v4/product/info/attributes", "/v5/product/info/prices"}
    assert not env["log"].exists()
    out = capsys.readouterr().out
    assert "MS-T1-BLACK-40: photos 3->10, video, rich, size-table, attrs [4495]" in out
    assert "DRY RUN" in out
    assert stats["offers"] == 3
    assert out.strip().splitlines()[-1].startswith("Done. 2 groups / 3 offers improved")


def test_snapshot_is_fresh(tmp_path):
    p = tmp_path / "s.json"
    assert not ic.snapshot_is_fresh(str(p))
    p.write_text("{}", encoding="utf-8")
    assert ic.snapshot_is_fresh(str(p))
    assert not ic.snapshot_is_fresh(str(p), today=date(2000, 1, 1))


def test_help_lists_required_flags(capsys):
    with pytest.raises(SystemExit):
        ic.parse_args(["--help"])
    out = capsys.readouterr().out
    for flag in ("--dry-run", "--limit-groups", "--max-minutes"):
        assert flag in out


def test_photo_target_takes_highest_unmet_tier():
    rec = {"groups": [{"key": "media", "unmet_conditions": [
        {"key": "media_images_1_2", "description": "Добавлено от 1 до 2 изображений"},
        {"key": "media_images_5_7", "description": "Добавлено от 5 до 7 изображений"},
        {"key": "media_images_8+", "description": "Добавлено от 8 изображений"},
        {"key": "media_video_or_video_cover", "description": "Добавлена видео или видеообложка"}]}]}
    assert ic.photo_target_from_conditions(rec) == 8
    assert ic.photo_target_from_conditions({"groups": []}) is None


def test_no_media_actions_without_usable_host_and_r2dev_links_count_as_missing():
    rating = {"groups": [{"key": "media", "unmet_conditions": [{"key": "media_images_8+", "description": "8"}]}]}
    record = {"images": ["a", "b"], "attributes": []}
    off = ic.select_actions(rating, record, False, media_ok=False)
    assert "photos" not in off and "video" not in off
    on = ic.select_actions(rating, record, False, media_ok=True)
    assert on["photos"] == 8 and on["video"] is True
    assert ic.r2_object_missing("https://pub-x.r2.dev/videos/a/video.mp4") is True
    assert ic.r2_object_missing("https://example.com/video.mp4") is False
    assert ic.r2_object_missing(None) is False
