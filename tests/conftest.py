import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_moderation_wait(monkeypatch):
    """run_new_products waits for Ozon moderation before checking refused cards; not in tests."""
    try:
        import run_new_products
    except Exception:
        return
    monkeypatch.setattr(run_new_products, "REJECT_CHECK_DELAY_S", 0)
