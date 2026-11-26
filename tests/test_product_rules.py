"""product_rules: owner's new-product rules (daily number, price limits, priorities) -> plan."""
import json

import product_rules as pr


def cand(brand, label, n, cost, key=None):
    return {"brand": brand, "label": label, "offers": n, "cost_try": cost, "key": key or f"{brand}{label}{n}{cost}",
            "name": "x", "url": "u"}


def rules(**kw):
    r = pr.default_rules()
    r.update(kw)
    return r


def test_allocate_by_weight_and_caps():
    a = pr.allocate(30, {"A": 100, "B": 100, "C": 100}, {"A": "top", "B": "medium", "C": "never"})
    assert a["C"] == 0 and a["A"] == 24 and a["B"] == 6          # 8:2
    a = pr.allocate(30, {"A": 5, "B": 100}, {"A": "urgent", "B": "low"})
    assert a == {"A": 5, "B": 25}                                 # A capped, rest to B
    assert pr.allocate(10, {"A": 3}, {})["A"] == 3                # default medium, capped
    assert sum(pr.allocate(0, {"A": 9}, {}).values()) == 0


def test_price_block():
    r = rules(min_sale_usd=40, max_cost_try=1500)
    assert pr.price_block(1000, r) is None            # $83
    assert pr.price_block(2000, r) == "cost above max"
    assert pr.price_block(400, r) == "sale price below min"   # $33.2
    assert pr.price_block(None, r) == "no price"
    assert pr.price_block(99999, rules()) is None     # 0 = no limits


def test_build_plan_whole_products_priorities_and_gap_fill():
    cands = [cand("M&S", "Underwear", 4, 1000, "u1"), cand("M&S", "Underwear", 4, 1000, "u2"),
             cand("M&S", "Underwear", 4, 1000, "u3"),
             cand("H&M", "Dress", 3, 500, "d1"), cand("H&M", "Dress", 3, 500, "d2"),
             cand("H&M", "Socks", 2, 100, "s1")]
    r = rules(daily_offers=10, priorities={"M&S/Underwear": "top", "H&M/Socks": "never"})
    p = pr.build_plan(r, cands)
    keys = [x["key"] for x in p["products"]]
    assert "s1" not in keys                             # never
    assert p["total"] <= 10
    assert keys[0] == "u1"                              # top first
    assert p["categories"][0]["label"] == "Underwear"   # ordered by priority
    assert p["total"] == 8      # underwear share 8 (2 products); dress share 2 < one 3-size product; never over


def test_gap_from_whole_products_is_filled_by_other_categories():
    cands = [cand("M&S", "Underwear", 3, 1000, "u1"), cand("M&S", "Underwear", 3, 1000, "u2"),
             cand("H&M", "Dress", 2, 500, "d1"), cand("H&M", "Dress", 2, 500, "d2"), cand("H&M", "Dress", 2, 500, "d3")]
    p = pr.build_plan(rules(daily_offers=10, priorities={"M&S/Underwear": "top"}), cands)
    # underwear share 8 -> 6 (two 3-size products), dress share 2 -> 2; gap 2 filled by another dress
    assert p["total"] == 10 and [x["key"] for x in p["products"]] == ["u1", "u2", "d1", "d2"]


def test_price_limits_filter_plan_and_report_blocked():
    cands = [cand("M&S", "Dress", 2, 3000, "exp"), cand("M&S", "Dress", 2, 1000, "ok")]
    p = pr.build_plan(rules(daily_offers=10, max_cost_try=2000), cands)
    assert [x["key"] for x in p["products"]] == ["ok"] and p["blocked"] == {"cost above max": 1}


def test_save_load_roundtrip_and_validation(tmp_path):
    path = str(tmp_path / "r.json")
    assert pr.load_rules(path) == pr.default_rules() and not pr.rules_exist(path)
    saved, errs = pr.save_rules(rules(daily_offers=50, min_sale_usd=30, max_cost_try=2500,
                                      priorities={"M&S/Underwear": "urgent", "H&M/Dress": "medium"}), path)
    assert errs == [] and saved["priorities"] == {"M&S/Underwear": "urgent"}   # medium = default, not stored
    r = pr.load_rules(path)
    assert r["daily_offers"] == 50 and pr.priority_of(r, "H&M", "Dress") == "medium"
    assert pr.save_rules(rules(daily_offers=-1), path)[1]
    assert pr.save_rules(rules(priorities={"x/y": "super"}), path)[1]
    assert pr.save_rules(rules(min_sale_usd=100, max_cost_try=1000), path)[1]   # 1000 TL = $83 < $100


def test_plan_day_from_rules_shape():
    day, plan = pr.plan_day_from_rules(rules(daily_offers=4), [cand("H&M", "Dress", 2, 500, "a"),
                                                              cand("H&M", "Dress", 2, 500, "b")])
    assert day == {"total": 4, "allocations": {"H&M": {"Dress": 4}}, "fill_remainder": False}


def test_live_ms_tokens_and_csv_candidates(tmp_path):
    p = tmp_path / "products.csv"
    p.write_text("url,parent_sku,ms_article_code,name,price,stock_count\n"
                 "a,111,T1,Külot,500,2\na,111,T1,Külot,500,0\na,111,T1,Külot,500,3\n"
                 "b,222,T2,Külot,500,1\nc,333,T3,Külot,500,0\n", encoding="utf-8-sig")
    tokens = pr.live_ms_tokens(["MS-T2-RED-S", "MS-999-34", "HM-1"])
    assert tokens == {"T2", "999"}
    out = pr.ms_candidates_from_csv(str(p), tokens, lambda n: "Underwear")
    assert [(c["key"], c["offers"]) for c in out] == [("a", 2)]   # b live, c sold out
