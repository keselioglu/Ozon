import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_moderation_wait(monkeypatch, tmp_path):
    """run_new_products waits for Ozon moderation before checking refused cards; not in tests."""
    try:
        import run_new_products
    except Exception:
        return
    monkeypatch.setattr(run_new_products, "REJECT_CHECK_DELAY_S", 0)
    # run() writes its state files in the cwd; a test once overwrote the real day's status
    # (2026-11-27 19:33: the app showed a "running" job that did not exist)
    for name in ("STATUS_FILE", "HISTORY_FILE", "DRYRUN_FILE", "LOCK_FILE"):
        monkeypatch.setattr(run_new_products, name, str(tmp_path / getattr(run_new_products, name)))
    # the photo retry calls Ozon; run() tests must not (its own test uses the saved original)
    if not hasattr(run_new_products, "_real_retry_failed_images"):
        run_new_products._real_retry_failed_images = run_new_products.retry_failed_images
    monkeypatch.setattr(run_new_products, "retry_failed_images", lambda items: [])
    if not hasattr(run_new_products, "_real_live_offer_ids_now"):
        run_new_products._real_live_offer_ids_now = run_new_products.live_offer_ids_now
    monkeypatch.setattr(run_new_products, "live_offer_ids_now", lambda: None)   # no Ozon paging
