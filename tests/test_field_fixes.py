"""Field-readiness fixes: regression tests for the five review findings.

1. Cross-marketplace realization haircut (Keepa/Amazon prices -> eBay sale)
2. Open-box Keepa fallback can actually produce a BUY (was mathematically
   impossible: derived+coarse+single-source penalties = 0.536 < 0.6 gate)
3. Unknown monthlySold != zero demand
4. Tiered margin thresholds by bin cost
5. labor_minutes migration + $/labor-hour report
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from binval.config import Thresholds, _DEFAULT_COSTS, load_settings
from binval.db import connect, labor_rate, record_outcome
from binval.engine import evaluate, gather_comps
from binval.keepa import KeepaSource, _ResponseCache
from binval.models import (
    CombinedComps,
    Condition,
    CostBreakdown,
    Identifier,
    IdentifierType,
    WeightClass,
)
from binval.verdict import confidence_score, decide
from tests.conftest import FakeCompSource

UPC = "036000291452"
FIXTURE = (Path(__file__).parent / "fixtures" / "keepa_product.json").read_bytes()


class _NoCache(_ResponseCache):
    def __init__(self):
        pass

    def get(self, key):
        return None

    def put(self, key, data):
        pass


def _keepa():
    return KeepaSource("TESTKEY", http_get=lambda url: FIXTURE, cache=_NoCache())


def _upc():
    return Identifier(IdentifierType.UPC, UPC, confidence=1.0)


# --- 1. realization haircut -------------------------------------------------

def test_keepa_comp_haircut_for_ebay_sale():
    combined, notes = gather_comps(
        [_keepa()], _upc(), Condition.NEW,
        sell_marketplace="ebay", amazon_to_ebay=0.75,
    )
    # fixture NEW avg90 = 39.99; eBay realization 0.75 -> 29.99
    assert combined is not None
    assert combined.median == pytest.approx(29.99, abs=0.01)
    assert "ebay_realization" in notes


def test_keepa_comp_unscaled_for_amazon_sale():
    combined, notes = gather_comps(
        [_keepa()], _upc(), Condition.NEW,
        sell_marketplace="amazon", amazon_to_ebay=0.75,
    )
    assert combined.median == 39.99
    assert "ebay_realization" not in notes


def test_ebay_side_source_never_haircut(make_comp):
    # ManualCompSource/Terapeak comps are already eBay prices.
    src = FakeCompSource("terapeak", {
        (UPC, Condition.USED): make_comp(40.0, 10, Condition.USED,
                                         source="terapeak"),
    })
    combined, notes = gather_comps(
        [src], _upc(), Condition.USED,
        sell_marketplace="ebay", amazon_to_ebay=0.75,
    )
    assert combined.median == 40.0   # untouched
    assert "ebay_realization" not in notes


# --- 2. open-box fallback can BUY (headline regression) ---------------------

def test_open_box_fallback_can_now_buy(settings):
    """End-to-end: Warehouse absent -> Used-Like-New fallback -> BUY.

    Before the fix, this path was capped at confidence 0.536 (< any sane
    gate) regardless of demand — the automated aisle loop could never fire
    on the most common bin-store condition. Uses an $80-list item so the
    economics genuinely clear the (unchanged, strict) net/margin gates."""
    # avg90: everything -1 except Used-Like-New (idx 19) = 7999 cents
    avg90 = [-1] * 20
    avg90[19] = 7999
    payload = json.dumps(
        {"products": [{"monthlySold": 14, "stats": {"avg90": avg90}}]}
    ).encode()
    src = KeepaSource("TESTKEY", http_get=lambda url: payload, cache=_NoCache())

    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        UPC, bin_cost=5.0, condition=Condition.OPEN_BOX,
        weight_class=WeightClass.UNDER_1LB,
        sources=[src], settings=settings, conn=conn,
    )
    assert comps is not None
    assert comps.derived is True
    # Like-New 79.99 * 0.75 realization = 59.99 resale
    assert comps.median == pytest.approx(59.99, abs=0.01)
    # 14 monthlySold -> base 0.9; x0.9 single source x0.7 derived = 0.567 >= 0.5
    assert verdict.confidence == pytest.approx(0.567, abs=0.001)
    # net = 59.99 - 5 - 7.80 fee - 1.80 pay - 6 ship - 0.75 mat - 7.20 defect
    assert verdict.net == pytest.approx(31.44, abs=0.01)
    assert verdict.decision == "BUY"


def test_derived_no_longer_double_penalized(make_comp):
    c_derived = CombinedComps(
        median=30.0, sold_count=25, condition=Condition.OPEN_BOX,
        sources=("keepa",), price_low=27.0, price_high=33.0,
        derived=True, coarse_match=False,
    )
    # base 1.0 x single 0.9 x derived 0.7 = 0.63 (not 0.536)
    assert confidence_score(c_derived, _upc()) == pytest.approx(0.63, abs=0.001)


# --- 3. unknown demand ------------------------------------------------------

def test_unknown_sold_count_gets_middle_base():
    c = CombinedComps(
        median=30.0, sold_count=0, condition=Condition.NEW,
        sources=("keepa",), price_low=27.0, price_high=33.0,
        sold_count_known=False,
    )
    # unknown base 0.6 x single-source 0.9 = 0.54 (clears the 0.5 gate)
    assert confidence_score(c, _upc()) == pytest.approx(0.54, abs=0.001)


def test_measured_zero_still_floor():
    c = CombinedComps(
        median=30.0, sold_count=0, condition=Condition.NEW,
        sources=("keepa",), price_low=27.0, price_high=33.0,
        sold_count_known=True,   # source measured zero recent sales
    )
    # measured-zero keeps the 0.4 floor x 0.9 = 0.36
    assert confidence_score(c, _upc()) == pytest.approx(0.36, abs=0.001)


def test_keepa_missing_monthly_sold_flagged_unknown():
    payload = b'{"products":[{"stats":{"avg90":[100,3999]}}]}'
    src = KeepaSource("TESTKEY", http_get=lambda url: payload, cache=_NoCache())
    r = src.get_comps(_upc(), Condition.NEW)
    assert r is not None
    assert r.sold_count == 0
    assert r.sold_count_known is False


# --- 4. tiered margins ------------------------------------------------------

TIERS = ((10.0, 3.0), (25.0, 2.0), (1_000_000.0, 1.5))


def _costs(net, bin_cost):
    return CostBreakdown(
        resale=0, bin_cost=bin_cost, marketplace_fee=0, payment_fee=0,
        shipping=0, materials=0, hazmat_penalty=0, defect_allowance=0, net=net,
    )


def test_margin_tier_selection():
    th = Thresholds(margin_tiers=TIERS)
    assert th.required_margin(5.0) == 3.0
    assert th.required_margin(10.0) == 3.0    # boundary inclusive
    assert th.required_margin(15.0) == 2.0
    assert th.required_margin(80.0) == 1.5


def test_higher_ticket_item_buys_at_lower_multiple():
    th = Thresholds(margin_tiers=TIERS, min_confidence=0.5)
    # $20 bin cost, $44 net -> 2.2x: fails flat 3x, passes the 2.0x tier
    v = decide(_costs(net=44.0, bin_cost=20.0), confidence=0.8, th=th)
    assert v.decision == "BUY"
    # same economics under flat 3x -> SKIP (what the old behavior did)
    v_flat = decide(_costs(net=44.0, bin_cost=20.0), confidence=0.8,
                    th=Thresholds())
    assert v_flat.decision == "SKIP"


def test_empty_tiers_fall_back_to_flat():
    th = Thresholds(min_margin_multiple=3.0, margin_tiers=())
    assert th.required_margin(50.0) == 3.0


def test_costs_toml_tiers_load():
    settings = load_settings("config/costs.toml")
    assert settings.thresholds.margin_tiers == TIERS
    assert settings.costs.amazon_to_ebay == 0.75
    assert settings.thresholds.min_confidence == 0.5


# --- 5. migration + labor rate ----------------------------------------------

_OLD_SCHEMA = """
CREATE TABLE scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    source_store TEXT, identifier_type TEXT NOT NULL, identifier TEXT NOT NULL,
    title TEXT, condition TEXT NOT NULL, category TEXT, marketplace TEXT,
    comp_median REAL, comp_count INTEGER, comp_source TEXT,
    bin_cost REAL NOT NULL, est_shipping REAL, est_defect_allowance REAL,
    est_net REAL, verdict TEXT NOT NULL, confidence REAL,
    actual_sold_price REAL, actual_ship_cost REAL, was_returned INTEGER,
    return_reason TEXT, actual_net REAL, days_to_sell INTEGER
);
"""


def test_old_db_migrates_labor_minutes(tmp_path):
    db = str(tmp_path / "old.db")
    raw = sqlite3.connect(db)
    raw.executescript(_OLD_SCHEMA)
    raw.execute(
        "INSERT INTO scans (identifier_type, identifier, condition, bin_cost,"
        " verdict, marketplace, category) VALUES ('upc', ?, 'open_box', 20.0,"
        " 'BUY', 'ebay', 'default')", (UPC,))
    raw.commit()
    raw.close()

    conn = connect(db)   # migration runs here
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scans)")}
    assert "labor_minutes" in cols
    # outcome with minutes works on the migrated row
    record_outcome(conn, 1, sold_price=95.0, ship_cost=11.0,
                   tables=_DEFAULT_COSTS, labor_minutes=30.0)
    rate = labor_rate(conn)
    assert rate is not None
    dollars_hr, hours, n = rate
    # actual_net 48.80 over 0.5h -> 97.60/hr
    assert dollars_hr == pytest.approx(97.60, abs=0.01)
    assert hours == 0.5
    assert n == 1


def test_labor_rate_none_without_minutes(settings):
    conn = connect(settings.db_path)
    assert labor_rate(conn) is None


# --- review hardening ---------------------------------------------------------

def test_margin_tiers_order_independent():
    # catch-all listed FIRST must not swallow the cheap tiers
    th = Thresholds(margin_tiers=((1_000_000.0, 1.5), (10.0, 3.0), (25.0, 2.0)))
    assert th.required_margin(5.0) == 3.0
    assert th.required_margin(15.0) == 2.0
    assert th.required_margin(80.0) == 1.5


def test_rerecording_outcome_keeps_labor_minutes(settings):
    conn = connect(settings.db_path)
    from tests.test_db import _seed_scan
    scan_id = _seed_scan(conn)
    record_outcome(conn, scan_id, sold_price=95.0, ship_cost=11.0,
                   tables=_DEFAULT_COSTS, labor_minutes=30.0)
    # correct the sale price later WITHOUT re-passing minutes
    record_outcome(conn, scan_id, sold_price=90.0, ship_cost=11.0,
                   tables=_DEFAULT_COSTS)
    row = conn.execute("SELECT labor_minutes FROM scans WHERE scan_id=?",
                       (scan_id,)).fetchone()
    assert row["labor_minutes"] == 30.0   # preserved, not nulled
