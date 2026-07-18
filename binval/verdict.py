"""Module 4 — Verdict.

PURE deterministic functions. Turns a CostBreakdown + comp confidence into a
BUY/SKIP call with the terse, glanceable output you read in the aisle.

Confidence weights here are v1 starting guesses, grouped as named constants so
the calibration data (Module 5) can justify or replace them later.
"""
from __future__ import annotations

from .config import Thresholds
from .models import (
    CombinedComps,
    CostBreakdown,
    Identifier,
    IdentifierType,
    Verdict,
)

# --- Confidence model (tunable constants) ----------------------------------

# Base confidence from how many recent sales back the comp. Unknown demand
# (source couldn't report a count, e.g. Keepa monthlySold absent) is NOT the
# same as measured-zero demand — it gets a middle base rather than the floor.
_BASE_UNKNOWN_COUNT = 0.6


def _base_from_sold_count(n: int, known: bool = True) -> float:
    if not known:
        return _BASE_UNKNOWN_COUNT
    if n >= 20:
        return 1.0
    if n >= 10:
        return 0.9
    if n >= 5:
        return 0.75
    if n >= 2:
        return 0.6
    return 0.4

# Multiplicative penalties for quality caveats.
_PENALTY_COARSE = 0.85       # Keepa "Used" blends subconditions
_PENALTY_SINGLE_SOURCE = 0.9  # only one sanctioned source agreed
_PENALTY_DIVERGENT = 0.7     # sources disagreed >40% at same condition
_PENALTY_TEXT_ID = 0.7       # matched on free-text title, not a barcode
_PENALTY_DERIVED = 0.7       # comp synthesized from a different condition


def confidence_score(comps: CombinedComps, identifier: Identifier) -> float:
    """Score in [0, 1]. Starts from sold-count tier, then applies caveats."""
    score = _base_from_sold_count(comps.sold_count, comps.sold_count_known)
    if comps.coarse_match:
        score *= _PENALTY_COARSE
    if len(comps.sources) < 2:
        score *= _PENALTY_SINGLE_SOURCE
    if comps.divergent:
        score *= _PENALTY_DIVERGENT
    if comps.derived:
        score *= _PENALTY_DERIVED
    if identifier.type == IdentifierType.TEXT:
        score *= _PENALTY_TEXT_ID
    # A free-text or check-digit-failed identifier carries its own confidence;
    # never let the final score exceed it.
    score = min(score, identifier.confidence)
    return round(score, 3)


def decide(costs: CostBreakdown, confidence: float, th: Thresholds,
           *, extra_reasons: tuple[str, ...] = ()) -> Verdict:
    """BUY iff net >= min_net AND margin_multiple >= min AND confidence >= min.
    Otherwise SKIP.

    `extra_reasons` are INFORMATIONAL flags (e.g. "battery: hazmat flag",
    "keepa_error"). They appear in the output but do NOT flip the decision —
    only the three threshold checks do. Hazmat cost is already baked into the
    landed-cost net, so a high-margin battery item can still be a BUY.
    """
    margin_multiple = (costs.net / costs.bin_cost) if costs.bin_cost > 0 else 0.0
    required_margin = th.required_margin(costs.bin_cost)
    fails: list[str] = []

    if costs.net < th.min_net:
        fails.append(f"net<${th.min_net:.0f}")
    if margin_multiple < required_margin:
        fails.append(f"margin<{required_margin:g}x")
    if confidence < th.min_confidence:
        fails.append(f"conf<{th.min_confidence:g}")

    decision = "BUY" if not fails else "SKIP"
    reasons = (*fails, *[r for r in extra_reasons if r not in fails])
    return Verdict(
        decision=decision,
        net=round(costs.net, 2),
        margin_multiple=round(margin_multiple, 2),
        confidence=round(confidence, 3),
        reasons=reasons,
    )


def format_terse(verdict: Verdict, comps: CombinedComps | None,
                 scan_id: int | None = None) -> str:
    """One-line aisle output. Examples:

      ✅ BUY  net +$14.20  4.1x  conf 0.82 | 12 sold @ ~$34.99 [keepa] #117
      ❌ SKIP net -$2.10   -0.4x conf 0.90 | margin<3x #118
    """
    icon = "✅" if verdict.decision == "BUY" else "❌"
    sign = "+" if verdict.net >= 0 else "-"
    net_str = f"{sign}${abs(verdict.net):.2f}"
    head = (f"{icon} {verdict.decision:<4} net {net_str:<9} "
            f"{verdict.margin_multiple:.1f}x  conf {verdict.confidence:.2f}")

    tail_parts: list[str] = []
    if comps is not None:
        src = "+".join(comps.sources)
        tail_parts.append(f"{comps.sold_count} sold @ ~${comps.median:.2f} [{src}]")
    if verdict.reasons:
        tail_parts.append(", ".join(verdict.reasons))
    if scan_id is not None:
        tail_parts.append(f"#{scan_id}")

    if tail_parts:
        return head + " | " + " ".join(tail_parts)
    return head
