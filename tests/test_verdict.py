"""Boundary tests for the verdict + confidence logic."""
from __future__ import annotations

import pytest

from binval.config import Thresholds
from binval.models import (
    CombinedComps,
    CostBreakdown,
    Condition,
    Identifier,
    IdentifierType,
)
from binval.verdict import confidence_score, decide, format_terse


def _comps(median=30.0, count=12, condition=Condition.OPEN_BOX,
           sources=("keepa",), divergent=False, coarse=False, derived=False):
    return CombinedComps(
        median=median, sold_count=count, condition=condition, sources=sources,
        price_low=median * 0.9, price_high=median * 1.1,
        divergent=divergent, coarse_match=coarse, derived=derived,
    )


def _costs(net=20.0, bin_cost=5.0):
    return CostBreakdown(
        resale=40.0, bin_cost=bin_cost, marketplace_fee=5.2, payment_fee=1.2,
        shipping=10.0, materials=0.75, hazmat_penalty=0.0, defect_allowance=4.0,
        net=net,
    )


def _upc(value="012345678905"):
    return Identifier(IdentifierType.UPC, value, confidence=1.0)


# --- confidence -------------------------------------------------------------

def test_confidence_sold_count_tiers():
    upc = _upc()
    # dual source removes the single-source penalty
    assert confidence_score(_comps(count=25, sources=("keepa", "terapeak")), upc) == 1.0
    assert confidence_score(_comps(count=15, sources=("keepa", "terapeak")), upc) == 0.9
    assert confidence_score(_comps(count=7, sources=("keepa", "terapeak")), upc) == 0.75
    assert confidence_score(_comps(count=3, sources=("keepa", "terapeak")), upc) == 0.6
    assert confidence_score(_comps(count=1, sources=("keepa", "terapeak")), upc) == 0.4


def test_confidence_single_source_penalty():
    upc = _upc()
    # 25 sold -> base 1.0, single source x0.9
    assert confidence_score(_comps(count=25, sources=("keepa",)), upc) == 0.9


def test_confidence_divergent_and_coarse_stack():
    upc = _upc()
    # base 1.0 (25), coarse 0.85, divergent 0.7, dual source (no single penalty)
    c = _comps(count=25, sources=("keepa", "terapeak"), divergent=True, coarse=True)
    assert confidence_score(c, upc) == pytest.approx(0.595, abs=1e-3)


def test_confidence_text_identifier_penalty_and_cap():
    # free-text id carries confidence 0.6; final score capped by it
    text_id = Identifier(IdentifierType.TEXT, "some title", confidence=0.6)
    c = _comps(count=25, sources=("keepa", "terapeak"))
    # base 1.0 x text penalty 0.7 = 0.7, but capped at identifier confidence 0.6
    assert confidence_score(c, text_id) == 0.6


def test_confidence_derived_penalty():
    upc = _upc()
    c = _comps(count=25, sources=("keepa",), derived=True)
    # base 1.0, single 0.9, derived 0.7 -> 0.63
    assert confidence_score(c, upc) == pytest.approx(0.63, abs=1e-3)


# --- decide -----------------------------------------------------------------

def test_decide_buy_when_all_thresholds_met():
    th = Thresholds()
    v = decide(_costs(net=20.0, bin_cost=5.0), confidence=0.8, th=th)
    assert v.decision == "BUY"
    assert v.margin_multiple == 4.0
    assert v.reasons == ()


def test_decide_skip_on_low_net():
    th = Thresholds()
    v = decide(_costs(net=9.99, bin_cost=1.0), confidence=0.9, th=th)
    assert v.decision == "SKIP"
    assert any("net<" in r for r in v.reasons)


def test_decide_skip_on_low_margin():
    th = Thresholds()
    # net 20, bin 10 -> 2.0x < 3.0x
    v = decide(_costs(net=20.0, bin_cost=10.0), confidence=0.9, th=th)
    assert v.decision == "SKIP"
    assert any("margin<" in r for r in v.reasons)


def test_decide_skip_on_low_confidence():
    th = Thresholds()  # default min_confidence now 0.5
    v = decide(_costs(net=50.0, bin_cost=5.0), confidence=0.49, th=th)
    assert v.decision == "SKIP"
    assert any("conf<" in r for r in v.reasons)


def test_decide_boundary_exactly_at_thresholds_is_buy():
    th = Thresholds(min_net=10.0, min_margin_multiple=3.0, min_confidence=0.6)
    # net exactly 10, bin cost so margin exactly 3.0x, confidence exactly 0.6
    v = decide(_costs(net=10.0, bin_cost=10.0 / 3.0), confidence=0.6, th=th)
    assert v.decision == "BUY"


def test_decide_zero_bin_cost_no_divzero():
    th = Thresholds()
    v = decide(_costs(net=50.0, bin_cost=0.0), confidence=0.9, th=th)
    # margin_multiple becomes 0.0, so SKIP on margin
    assert v.margin_multiple == 0.0
    assert v.decision == "SKIP"


# --- format -----------------------------------------------------------------

def test_format_terse_buy_line():
    th = Thresholds()
    v = decide(_costs(net=14.20, bin_cost=3.46), confidence=0.82, th=th)
    line = format_terse(v, _comps(median=34.99, count=12), scan_id=117)
    assert line.startswith("✅ BUY")
    assert "+$14.20" in line
    assert "#117" in line
    assert "[keepa]" in line


def test_format_terse_skip_shows_reasons():
    th = Thresholds()
    v = decide(_costs(net=-2.10, bin_cost=5.0), confidence=0.9, th=th)
    line = format_terse(v, None, scan_id=118)
    assert line.startswith("❌ SKIP")
    assert "-$2.10" in line
    assert "#118" in line
