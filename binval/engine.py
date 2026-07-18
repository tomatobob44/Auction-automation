"""Engine — orchestrates Modules 1 -> 2 -> 3 -> 4 -> 5.

The only place sources, config, and the db connection get wired together.
Source failures (network/auth) are caught and degraded to "no comp from that
source" so one flaky API never crashes a valuation.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace

from .comps import CompSource, combine_comps
from .config import Settings
from .costs import landed_net
from .db import log_scan
from .identify import classify, decode_barcode
from .models import (
    CombinedComps,
    Condition,
    CostBreakdown,
    Identifier,
    IdentifierType,
    Verdict,
    WeightClass,
)
from .verdict import confidence_score, decide

log = logging.getLogger(__name__)


def identify_input(raw_input: str | bytes) -> Identifier:
    """Turn a raw string or image bytes into a typed Identifier."""
    if isinstance(raw_input, (bytes, bytearray)):
        ident = decode_barcode(bytes(raw_input))
        if ident is None:
            # Could not decode the image — unknown, zero confidence.
            return Identifier(IdentifierType.TEXT, value="", confidence=0.0)
        return ident
    return classify(raw_input)


def gather_comps(
    sources: list[CompSource],
    identifier: Identifier,
    condition: Condition,
    *,
    sell_marketplace: str = "ebay",
    amazon_to_ebay: float = 1.0,
) -> tuple[CombinedComps | None, list[str]]:
    """Query each source for the requested condition and combine
    same-condition comps. Returns (combined, notes); notes flag source
    failures so they surface in the verdict reasons.

    Cross-marketplace correction: an Amazon-side comp (Keepa) used to price an
    eBay sale is scaled by `amazon_to_ebay` BEFORE combining — Amazon listing
    prices systematically overstate eBay realized prices for used/open-box
    goods. Same-marketplace comps pass through untouched.
    """
    results = []
    notes: list[str] = []
    for src in sources:
        try:
            r = src.get_comps(identifier, condition)
            if r is not None:
                results.append(r)
        except Exception as e:  # network/auth/parse — degrade gracefully
            notes.append(f"{getattr(src, 'name', 'source')}_error")
            log.warning("comp source %s failed: %s",
                        getattr(src, "name", "?"), e)

    if sell_marketplace.lower() == "ebay" and amazon_to_ebay != 1.0:
        adjusted = []
        for r in results:
            if r.domain == "amazon":
                adjusted.append(replace(
                    r,
                    median_sold_price=round(r.median_sold_price * amazon_to_ebay, 2),
                    price_low=round(r.price_low * amazon_to_ebay, 2),
                    price_high=round(r.price_high * amazon_to_ebay, 2),
                ))
                if "ebay_realization" not in notes:
                    notes.append("ebay_realization")
            else:
                adjusted.append(r)
        results = adjusted

    combined = combine_comps(results, condition)
    return combined, notes


def evaluate(
    raw_input: str | bytes,
    *,
    bin_cost: float,
    condition: Condition = Condition.OPEN_BOX,
    weight_class: WeightClass = WeightClass.LB_1_3,
    marketplace: str = "ebay",
    category: str = "default",
    lithium: bool = False,
    oversize: bool = False,
    sources: list[CompSource],
    settings: Settings,
    conn: sqlite3.Connection | None = None,
    source_store: str | None = None,
) -> tuple[Verdict, CombinedComps | None, CostBreakdown, int | None]:
    """Full single-item valuation: identify -> comps -> landed cost ->
    verdict -> log. Returns (verdict, combined_comps, cost_breakdown, scan_id).

    Default condition is OPEN_BOX — bin-store stock is overwhelmingly
    returns/open-box even when it looks new; pricing as NEW is how you overpay.
    """
    identifier = identify_input(raw_input)
    combined, notes = gather_comps(
        sources, identifier, condition,
        sell_marketplace=marketplace,
        amazon_to_ebay=settings.costs.amazon_to_ebay,
    )

    # Informational flags that must surface even on a BUY.
    info_flags: tuple[str, ...] = tuple(notes)
    if lithium:
        info_flags = (*info_flags, "battery: hazmat flag")

    if combined is None:
        # No clean comp at this condition -> SKIP. Don't guess.
        costs = landed_net(
            0.0, bin_cost, marketplace=marketplace, category=category,
            weight_class=weight_class, condition=condition,
            lithium=lithium, oversize=oversize, tables=settings.costs,
        )
        verdict = Verdict(
            decision="SKIP", net=costs.net, margin_multiple=0.0,
            confidence=0.0, reasons=("no clean comp", *info_flags),
        )
    else:
        costs = landed_net(
            combined.median, bin_cost,
            marketplace=marketplace, category=category,
            weight_class=weight_class, condition=condition,
            lithium=lithium, oversize=oversize, tables=settings.costs,
        )
        conf = confidence_score(combined, identifier)
        verdict = decide(costs, conf, settings.thresholds,
                         extra_reasons=info_flags)

    scan_id = None
    if conn is not None:
        scan_id = log_scan(
            conn, identifier=identifier, condition=condition,
            category=category, marketplace=marketplace, comps=combined,
            costs=costs, verdict=verdict, source_store=source_store,
        )
    return verdict, combined, costs, scan_id
