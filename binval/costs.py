"""Module 3 — Landed-cost model.

PURE deterministic functions. No model calls, no API calls, no I/O. This is
the part that prevents bankruptcy: the verdict is based on net dollars you
actually keep, never the resale sticker price.

Designed to run unchanged on a local agent (Clawd) at $0 marginal cost.
"""
from __future__ import annotations

from .config import CostTables
from .models import Condition, CostBreakdown, WeightClass


def marketplace_fee(resale: float, marketplace: str, category: str,
                    tables: CostTables) -> float:
    """Final value / referral fee in dollars for a given sale price."""
    rate = tables.marketplace_fee_rate(marketplace, category)
    return round(resale * rate, 2)


def payment_fee(resale: float, marketplace: str, tables: CostTables) -> float:
    """Payment processing in dollars. Amazon folds this into its referral
    fee, so it is only charged for marketplaces that don't bundle it."""
    if marketplace.lower() == "amazon":
        return 0.0
    return round(resale * tables.payment_rate, 2)


def shipping_estimate(weight_class: WeightClass, tables: CostTables) -> float:
    """Outbound shipping estimate from the weight class. Values in the table
    are biased HIGH on purpose — under-estimating shipping books a fake
    profit that is really a loss."""
    return tables.shipping[weight_class]


def defect_allowance(resale: float, condition: Condition, shipping: float,
                     tables: CostTables) -> float:
    """Expected cost of returns / INAD, scaled by condition.

    On an "item not as described" you typically eat the item cost AND pay
    shipping twice (outbound + return), so the exposure per sale is
    (resale + 2*shipping), multiplied by the condition's return rate.
    """
    rate = tables.defect_rates.get(condition, 0.0)
    exposure = resale + 2.0 * shipping
    return round(rate * exposure, 2)


def landed_net(
    resale: float,
    bin_cost: float,
    *,
    marketplace: str,
    category: str,
    weight_class: WeightClass,
    condition: Condition,
    lithium: bool = False,
    oversize: bool = False,
    tables: CostTables,
) -> CostBreakdown:
    """Compute the full landed-cost breakdown and the bottom-line net.

    net = resale
        - bin_cost
        - marketplace_fee
        - payment_fee
        - shipping
        - materials
        - hazmat/oversize penalties
        - defect_allowance
    """
    mkt_fee = marketplace_fee(resale, marketplace, category, tables)
    pay_fee = payment_fee(resale, marketplace, tables)
    shipping = shipping_estimate(weight_class, tables)
    materials = tables.materials

    hazmat = 0.0
    if lithium:
        hazmat += tables.lithium_penalty
    if oversize:
        hazmat += tables.oversize_penalty
    hazmat = round(hazmat, 2)

    defect = defect_allowance(resale, condition, shipping, tables)

    net = (
        resale
        - bin_cost
        - mkt_fee
        - pay_fee
        - shipping
        - materials
        - hazmat
        - defect
    )

    return CostBreakdown(
        resale=round(resale, 2),
        bin_cost=round(bin_cost, 2),
        marketplace_fee=mkt_fee,
        payment_fee=pay_fee,
        shipping=round(shipping, 2),
        materials=round(materials, 2),
        hazmat_penalty=hazmat,
        defect_allowance=defect,
        net=round(net, 2),
    )
