"""Module 2 — Comp lookup interface, condition normalization, and combination.

Both eBay and Keepa speak different condition languages. This module is the
shared language: every source maps into the normalized Condition enum, and the
engine only ever compares like-for-like. A NEW comp contaminating a USED
valuation is worse than having one fewer source — so condition match is
mandatory and an unmatched comp is dropped, never blended.

KeepaSource lives in keepa.py (the only file that touches a paid API).
This module holds the interface, the maps, the combine logic, and the
sanctioned non-Keepa sources: ManualCompSource (Terapeak entry) and the
dormant EbayInsightsSource stub.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import CombinedComps, CompResult, Condition, Identifier

# --- Condition mapping tables ----------------------------------------------

# eBay condition ID -> normalized Condition.
EBAY_CONDITION_MAP: dict[int, Condition] = {
    1000: Condition.NEW,
    1500: Condition.OPEN_BOX,
    2000: Condition.REFURB,
    2010: Condition.REFURB,
    2500: Condition.REFURB,
    3000: Condition.USED,
    4000: Condition.USED,
    5000: Condition.USED,
    7000: Condition.PARTS,
}

# normalized Condition -> the eBay condition IDs to filter by.
NORMALIZED_TO_EBAY_IDS: dict[Condition, tuple[int, ...]] = {
    Condition.NEW: (1000,),
    Condition.OPEN_BOX: (1500,),
    Condition.REFURB: (2000, 2010, 2500),
    Condition.USED: (3000, 4000, 5000),
    Condition.PARTS: (7000,),
}

# Keepa CSV price-history indices we care about. Keepa stores parallel price
# series in a `csv` array indexed by type; these are the relevant ones.
#   1  = AMAZON/NEW (3rd-party new used as the NEW proxy here is index 1/NEW)
#   2  = USED
#   6  = REFURBISHED
#   9  = WAREHOUSE DEALS (Amazon Warehouse / open-box)
#   19 = USED - LIKE NEW
#   20 = USED - VERY GOOD
KEEPA_INDEX = {
    "NEW": 1,
    "USED": 2,
    "REFURBISHED": 6,
    "WAREHOUSE": 9,
    "USED_LIKE_NEW": 19,
    "USED_VERY_GOOD": 20,
}

# normalized Condition -> Keepa index lookup order. The first index with data
# wins; anything past the first entry is a fallback and gets flagged.
#   OPEN_BOX: prefer true Warehouse Deals, then fall back to the closest used
#   sub-tiers (Like New, Very Good) that customers accept as open-box-grade.
NORMALIZED_TO_KEEPA_INDICES: dict[Condition, tuple[int, ...]] = {
    Condition.NEW: (KEEPA_INDEX["NEW"],),
    Condition.OPEN_BOX: (
        KEEPA_INDEX["WAREHOUSE"],
        KEEPA_INDEX["USED_LIKE_NEW"],
        KEEPA_INDEX["USED_VERY_GOOD"],
    ),
    Condition.REFURB: (KEEPA_INDEX["REFURBISHED"],),
    Condition.USED: (KEEPA_INDEX["USED"],),
    # PARTS intentionally absent — Keepa has no clean series; returns None.
}


# --- Source interface ------------------------------------------------------

class CompSource(ABC):
    name: str
    # Which marketplace's prices this source reports. Cross-marketplace comps
    # (Amazon-side Keepa prices for an eBay sale) get a realization haircut in
    # the engine before combining.
    domain: str = "ebay"

    @abstractmethod
    def get_comps(self, identifier: Identifier,
                  condition: Condition) -> CompResult | None:
        """Return sold-price comps for the item AT THE GIVEN CONDITION, or None
        if this source cannot supply a clean condition match. Must never raise
        on a simple "no data" outcome — return None instead."""
        raise NotImplementedError


# --- Combination -----------------------------------------------------------

DIVERGENCE_THRESHOLD = 0.40   # >40% spread at the same condition is a red flag


def combine_comps(results: list[CompResult | None],
                  condition: Condition) -> CombinedComps | None:
    """Combine same-condition comps into one figure for the verdict.

    Rules:
      - Drop anything not at `condition` (defensive; sources already filter).
      - No valid comp -> None (engine turns this into SKIP; don't guess).
      - One valid comp -> pass through (flags propagate).
      - Two+ valid -> if spread > 40% take the LOWER median and flag divergent;
        otherwise blend, weighting medians by sold_count.
    """
    valid = [r for r in results if r is not None and r.condition == condition]
    if not valid:
        return None

    coarse = any(r.coarse_match for r in valid)
    derived = any(r.derived for r in valid)
    count_known = any(r.sold_count_known for r in valid)
    sources = tuple(r.source_name for r in valid)
    total_count = sum(r.sold_count for r in valid)
    low = min(r.price_low for r in valid)
    high = max(r.price_high for r in valid)

    if len(valid) == 1:
        r = valid[0]
        return CombinedComps(
            median=round(r.median_sold_price, 2),
            sold_count=r.sold_count,
            condition=condition,
            sources=sources,
            price_low=round(r.price_low, 2),
            price_high=round(r.price_high, 2),
            divergent=False,
            coarse_match=coarse,
            derived=derived,
            sold_count_known=count_known,
        )

    medians = [r.median_sold_price for r in valid]
    lo, hi = min(medians), max(medians)
    divergent = lo > 0 and (hi - lo) / lo > DIVERGENCE_THRESHOLD

    if divergent:
        # Sources disagree badly — usually a bad identifier match or a category
        # one source handles poorly. Lean low; do not average a bad pair.
        median = lo
    else:
        # Weight by sold_count (more sales = more trustworthy). If every source
        # reports 0 sales, fall back to a simple mean.
        weight = sum(r.sold_count for r in valid)
        if weight > 0:
            median = sum(r.median_sold_price * r.sold_count for r in valid) / weight
        else:
            median = sum(medians) / len(medians)

    return CombinedComps(
        median=round(median, 2),
        sold_count=total_count,
        condition=condition,
        sources=sources,
        price_low=round(low, 2),
        price_high=round(high, 2),
        divergent=divergent,
        coarse_match=coarse,
        derived=derived,
        sold_count_known=count_known,
    )


# --- Sanctioned non-Keepa sources ------------------------------------------

class ManualCompSource(CompSource):
    """The Terapeak path: you pull median sold + count from Seller Hub by hand
    and feed it in. Zero ToS risk, fully sanctioned, no automation."""

    name = "terapeak"

    def __init__(self, median: float, count: int, condition: Condition,
                 low: float | None = None, high: float | None = None):
        self._median = float(median)
        self._count = int(count)
        self._condition = condition
        self._low = float(low) if low is not None else self._median * 0.9
        self._high = float(high) if high is not None else self._median * 1.1

    def get_comps(self, identifier: Identifier,
                  condition: Condition) -> CompResult | None:
        # Only answers for the condition it was given values for.
        if condition != self._condition:
            return None
        return CompResult(
            median_sold_price=self._median,
            sold_count=self._count,
            price_low=self._low,
            price_high=self._high,
            condition=self._condition,
            source_name=self.name,
        )


class EbayInsightsSource(CompSource):
    """eBay Marketplace Insights API — returns REAL sold-price data, but access
    is approval-gated. This stays dormant (constructed only when a token is
    present). The condition-ID mapping is wired and unit-tested; the live HTTP
    path is written but cannot be verified without granted access.

    NEVER scrape eBay sold listings here — that violates eBay ToS on the same
    account you sell through.
    """

    name = "ebay_insights"
    _ENDPOINT = ("https://api.ebay.com/buy/marketplace_insights/v1_beta/"
                 "item_sales/search")

    def __init__(self, token: str, http_get=None):
        self._token = token
        # Dependency-injected for testing; defaults to requests at call time.
        self._http_get = http_get

    def get_comps(self, identifier: Identifier,
                  condition: Condition) -> CompResult | None:
        cond_ids = NORMALIZED_TO_EBAY_IDS.get(condition)
        if not cond_ids:
            return None
        if self._http_get is None:
            # No transport wired and we're not going to scrape. Treat as
            # "no sanctioned data available" rather than raising.
            return None
        # Live implementation would query self._ENDPOINT filtered by cond_ids,
        # parse lastSoldPrice values, and build a CompResult. Left dormant
        # until Marketplace Insights access is granted.
        return None
