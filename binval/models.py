"""Shared enums and frozen dataclasses used across all modules.

Pure stdlib — no imports from other binval modules, no external deps.
Everything downstream (costs, verdict, db, engine) depends on these types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Condition(str, Enum):
    """Normalized condition scale. Both eBay and Keepa map INTO this; the
    engine only ever values against a normalized condition, never a
    source-specific one."""
    NEW = "new"
    OPEN_BOX = "open_box"
    REFURB = "refurb"
    USED = "used"
    PARTS = "parts"


class IdentifierType(str, Enum):
    UPC = "upc"      # 12-digit UPC-A
    EAN = "ean"      # 13-digit EAN-13
    ASIN = "asin"    # Amazon Standard Identification Number
    TEXT = "text"    # free-text title (lower confidence)


class WeightClass(str, Enum):
    """Shipping weight buckets. Map → carrier cost estimate (biased high)."""
    UNDER_1LB = "under_1lb"
    LB_1_3 = "lb_1_3"
    LB_3_10 = "lb_3_10"
    OVER_10LB = "over_10lb"


@dataclass(frozen=True)
class Identifier:
    type: IdentifierType
    value: str
    raw_title: str | None = None
    # 1.0 for a clean barcode/UPC/ASIN; lower for free-text or a UPC that
    # failed its check digit.
    confidence: float = 1.0


@dataclass(frozen=True)
class CompResult:
    """Sold-price comps for an item AT A SPECIFIC normalized condition.
    `condition` MUST equal the condition that was requested."""
    median_sold_price: float
    sold_count: int
    price_low: float
    price_high: float
    condition: Condition
    source_name: str
    # Keepa "Used" blends Like-New/Very-Good/Good/Acceptable; set when the
    # source could not give an exact subcondition match.
    coarse_match: bool = False
    # Set when the comp was derived from a different condition (e.g. OPEN_BOX
    # synthesized from a Used sub-tier because Warehouse Deals had no data).
    derived: bool = False


@dataclass(frozen=True)
class CombinedComps:
    """Result of combining same-condition comps from one or more sources.
    Input to the verdict step."""
    median: float
    sold_count: int
    condition: Condition
    sources: tuple[str, ...]
    price_low: float
    price_high: float
    divergent: bool = False       # cross-source spread > 40% at same condition
    coarse_match: bool = False
    derived: bool = False


@dataclass(frozen=True)
class CostBreakdown:
    """Auditable line-by-line landed cost. `net` is the only number the
    verdict ultimately cares about, but every component is retained for
    logging and the terse aisle output."""
    resale: float
    bin_cost: float
    marketplace_fee: float
    payment_fee: float
    shipping: float
    materials: float
    hazmat_penalty: float
    defect_allowance: float
    net: float


@dataclass(frozen=True)
class Verdict:
    decision: str                 # "BUY" | "SKIP"
    net: float
    margin_multiple: float
    confidence: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
