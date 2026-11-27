"""--test-offers N: a real one-off run of N offers under the saved rules (owner, 2026-11-27)."""
import pytest

import daily_plan
import product_rules as pr
import run_new_products as rnp
import upload_to_ozon


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(pr, "rules_exist", lambda path=None: True)
    monkeypatch.setattr(pr, "load_rules", lambda path=None: dict(pr.default_rules(), daily_offers=500,
                                                                    priorities={"M&S/Underwear": "top"}))
    cands = [{"brand": "M&S", "label": "Underwear", "offers": 2, "cost_try": 1000, "key": f"u{i}", "name": "x",
              "url": "u"} for i in range(200)]
    monkeypatch.setattr(pr, "load_candidates", lambda *a: cands)
    monkeypatch.setattr(upload_to_ozon, "check_quota", lambda: {"daily_create": {"limit": 2000, "usage": 600}})
    monkeypatch.setattr(daily_plan, "created_today", lambda today=None: (500, {("M&S", "Underwear"): 500}))


def test_test_offers_plans_exactly_n_and_ignores_todays_uploads(env):
    remaining, want, _ = rnp.targets_for_today("2026-11-27", test_offers=100)
    assert remaining["plan_total"] == 100 and remaining["created_before"] == 0
    assert remaining["total"] == 100 and sum(want.values()) == 100


def test_normal_run_counts_todays_uploads(env):
    remaining, _, _ = rnp.targets_for_today("2026-11-27")
    assert remaining["plan_total"] == 500 and remaining["total"] == 0   # 500 already created today


def test_cli_rejects_both_test_flags():
    with pytest.raises(SystemExit):
        rnp.main(["--test", "10", "--test-offers", "100"])
