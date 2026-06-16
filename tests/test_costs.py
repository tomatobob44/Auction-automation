"""Exact-number tests for the landed-cost math. Highest-value tests in the
suite — these are the numbers that decide whether money is made or lost."""
from __future__ import annotations

import pytest

from binval.costs import (
    defect_allowance,
    landed_net,
    marketplace_fee,
    payment_fee,
    shipping_estimate,
)
from binval.models import Condition, WeightClass


def test_marketplace_fee_ebay_default(tables):
    # 13% of $100
    assert marketplace_fee(100.0, "ebay", "default", tables) == 13.0


def test_marketplace_fee_amazon_category_override(tables):
    # electronics override = 8%
    assert marketplace_fee(50.0, "amazon", "electronics", tables) == 4.0
    # unknown category falls back to amazon default 15%
    assert marketplace_fee(50.0, "amazon", "nonsense", tables) == 7.5


def test_payment_fee_only_when_not_amazon(tables):
    assert payment_fee(100.0, "ebay", tables) == 3.0       # 3%
    assert payment_fee(100.0, "amazon", tables) == 0.0     # bundled


def test_shipping_estimate_weight_classes(tables):
    assert shipping_estimate(WeightClass.UNDER_1LB, tables) == 6.0
    assert shipping_estimate(WeightClass.LB_1_3, tables) == 10.0
    assert shipping_estimate(WeightClass.LB_3_10, tables) == 16.0
    assert shipping_estimate(WeightClass.OVER_10LB, tables) == 28.0


def test_defect_allowance_accounts_for_double_shipping(tables):
    # OPEN_BOX rate 0.10, resale 100, shipping 10
    # exposure = 100 + 2*10 = 120; allowance = 0.10 * 120 = 12.00
    assert defect_allowance(100.0, Condition.OPEN_BOX, 10.0, tables) == 12.0


def test_defect_allowance_new_low(tables):
    # NEW rate 0.025, resale 200, shipping 6 -> 0.025*(200+12)=5.30
    assert defect_allowance(200.0, Condition.NEW, 6.0, tables) == 5.3


def test_landed_net_full_breakdown(tables):
    b = landed_net(
        100.0, 20.0,
        marketplace="ebay", category="default",
        weight_class=WeightClass.LB_1_3,
        condition=Condition.OPEN_BOX,
        tables=tables,
    )
    assert b.marketplace_fee == 13.0       # 13%
    assert b.payment_fee == 3.0            # 3%
    assert b.shipping == 10.0
    assert b.materials == 0.75
    assert b.hazmat_penalty == 0.0
    # defect = 0.10 * (100 + 2*10) = 12.0
    assert b.defect_allowance == 12.0
    # net = 100 - 20 - 13 - 3 - 10 - 0.75 - 0 - 12 = 41.25
    assert b.net == 41.25


def test_landed_net_lithium_flag_adds_penalty(tables):
    b = landed_net(
        30.0, 5.0,
        marketplace="ebay", category="default",
        weight_class=WeightClass.UNDER_1LB,
        condition=Condition.OPEN_BOX,
        lithium=True,
        tables=tables,
    )
    assert b.hazmat_penalty == 22.5
    # fee 3.9, pay 0.9, ship 6, mat 0.75, hazmat 22.5
    # defect 0.10*(30+12)=4.2
    # net = 30 - 5 - 3.9 - 0.9 - 6 - 0.75 - 22.5 - 4.2 = -13.25
    assert b.net == -13.25


def test_landed_net_oversize_and_lithium_stack(tables):
    b = landed_net(
        100.0, 10.0,
        marketplace="ebay", category="default",
        weight_class=WeightClass.OVER_10LB,
        condition=Condition.USED,
        lithium=True, oversize=True,
        tables=tables,
    )
    assert b.hazmat_penalty == 37.5    # 22.5 + 15.0


def test_landed_net_amazon_no_payment_fee(tables):
    b = landed_net(
        100.0, 10.0,
        marketplace="amazon", category="default",
        weight_class=WeightClass.UNDER_1LB,
        condition=Condition.NEW,
        tables=tables,
    )
    assert b.payment_fee == 0.0
    assert b.marketplace_fee == 15.0
