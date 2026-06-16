"""Condition mapping + combine logic. Uses hand-built CompResults; no network."""
from __future__ import annotations

import pytest

from binval.comps import (
    DIVERGENCE_THRESHOLD,
    EBAY_CONDITION_MAP,
    NORMALIZED_TO_EBAY_IDS,
    NORMALIZED_TO_KEEPA_INDICES,
    ManualCompSource,
    combine_comps,
)
from binval.models import Condition, Identifier, IdentifierType


# --- mapping tables ---------------------------------------------------------

def test_ebay_condition_map_covers_spec_ids():
    assert EBAY_CONDITION_MAP[1000] == Condition.NEW
    assert EBAY_CONDITION_MAP[1500] == Condition.OPEN_BOX
    assert EBAY_CONDITION_MAP[2000] == Condition.REFURB
    assert EBAY_CONDITION_MAP[2010] == Condition.REFURB
    assert EBAY_CONDITION_MAP[3000] == Condition.USED
    assert EBAY_CONDITION_MAP[4000] == Condition.USED
    assert EBAY_CONDITION_MAP[5000] == Condition.USED
    assert EBAY_CONDITION_MAP[7000] == Condition.PARTS


def test_normalized_to_ebay_ids_roundtrip():
    # every id we filter by maps back to the same normalized condition
    for cond, ids in NORMALIZED_TO_EBAY_IDS.items():
        for i in ids:
            assert EBAY_CONDITION_MAP[i] == cond


def test_keepa_open_box_fallback_order():
    # OPEN_BOX prefers Warehouse (9), then Used-Like-New (19), Used-Very-Good (20)
    assert NORMALIZED_TO_KEEPA_INDICES[Condition.OPEN_BOX] == (9, 19, 20)
    # PARTS has no Keepa series
    assert Condition.PARTS not in NORMALIZED_TO_KEEPA_INDICES


# --- combine: single & none -------------------------------------------------

def test_combine_none_when_no_valid(make_comp):
    assert combine_comps([], Condition.USED) is None
    assert combine_comps([None, None], Condition.USED) is None


def test_combine_drops_wrong_condition(make_comp):
    new_comp = make_comp(50.0, 10, Condition.NEW)
    # asking for USED but only a NEW comp exists -> None
    assert combine_comps([new_comp], Condition.USED) is None


def test_combine_single_passthrough_propagates_flags(make_comp):
    c = make_comp(40.0, 8, Condition.USED, coarse=True, derived=True)
    out = combine_comps([c], Condition.USED)
    assert out is not None
    assert out.median == 40.0
    assert out.sold_count == 8
    assert out.coarse_match is True
    assert out.derived is True
    assert out.divergent is False
    assert out.sources == ("fake",)


# --- combine: two sources ---------------------------------------------------

def test_combine_blends_weighted_by_sold_count(make_comp):
    # close medians (within 40%): weight by count
    a = make_comp(30.0, 10, Condition.OPEN_BOX, source="keepa")
    b = make_comp(40.0, 30, Condition.OPEN_BOX, source="terapeak")
    out = combine_comps([a, b], Condition.OPEN_BOX)
    # (30*10 + 40*30) / 40 = (300 + 1200)/40 = 37.5
    assert out.median == 37.5
    assert out.sold_count == 40
    assert out.divergent is False
    assert set(out.sources) == {"keepa", "terapeak"}


def test_combine_divergent_takes_lower_value(make_comp):
    # 100 vs 50 -> spread (100-50)/50 = 1.0 > 0.40 -> divergent, take lower
    a = make_comp(50.0, 5, Condition.USED, source="keepa")
    b = make_comp(100.0, 5, Condition.USED, source="terapeak")
    out = combine_comps([a, b], Condition.USED)
    assert out.divergent is True
    assert out.median == 50.0


def test_combine_just_under_divergence_blends(make_comp):
    # spread exactly at 0.40 is NOT divergent (> threshold required)
    a = make_comp(100.0, 10, Condition.USED, source="keepa")
    b = make_comp(140.0, 10, Condition.USED, source="terapeak")
    spread = (140.0 - 100.0) / 100.0
    assert spread == pytest.approx(DIVERGENCE_THRESHOLD)
    out = combine_comps([a, b], Condition.USED)
    assert out.divergent is False
    assert out.median == 120.0   # equal weights -> mean


def test_combine_zero_counts_falls_back_to_mean(make_comp):
    # close medians (non-divergent) with zero sold counts -> simple mean
    a = make_comp(30.0, 0, Condition.USED, source="keepa")
    b = make_comp(36.0, 0, Condition.USED, source="terapeak")
    out = combine_comps([a, b], Condition.USED)
    assert out.divergent is False
    assert out.median == 33.0


# --- ManualCompSource (Terapeak) -------------------------------------------

def test_manual_source_only_answers_its_condition():
    src = ManualCompSource(median=34.99, count=12, condition=Condition.OPEN_BOX)
    ident = Identifier(IdentifierType.UPC, "012345678905")
    hit = src.get_comps(ident, Condition.OPEN_BOX)
    assert hit is not None
    assert hit.median_sold_price == 34.99
    assert hit.sold_count == 12
    assert hit.condition == Condition.OPEN_BOX
    # different condition -> None
    assert src.get_comps(ident, Condition.USED) is None
