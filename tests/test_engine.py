"""End-to-end evaluate() runs with FakeCompSource — no network, no API keys."""
from __future__ import annotations

import pytest

from binval.db import connect
from binval.engine import evaluate
from binval.models import Condition, WeightClass
from tests.conftest import FakeCompSource


VALID_UPC = "036000291452"


def test_evaluate_buy_logs_scan(settings, make_comp):
    src = FakeCompSource("keepa", {
        (VALID_UPC, Condition.OPEN_BOX): make_comp(
            100.0, 15, Condition.OPEN_BOX, source="keepa"),
    })
    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        VALID_UPC, bin_cost=10.0, condition=Condition.OPEN_BOX,
        weight_class=WeightClass.LB_1_3, sources=[src], settings=settings,
        conn=conn, source_store="Test Bin",
    )
    assert verdict.decision == "BUY"
    assert comps is not None and comps.median == 100.0
    assert scan_id is not None
    row = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
    assert row["verdict"] == "BUY"
    assert row["comp_source"] == "keepa"


def test_evaluate_no_comp_is_skip(settings):
    src = FakeCompSource("keepa", {})  # returns None for everything
    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        VALID_UPC, bin_cost=5.0, condition=Condition.OPEN_BOX,
        sources=[src], settings=settings, conn=conn,
    )
    assert verdict.decision == "SKIP"
    assert comps is None
    assert "no clean comp" in verdict.reasons
    # still logged
    row = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
    assert row["verdict"] == "SKIP"
    assert row["comp_median"] is None


def test_evaluate_source_failure_degrades(settings, make_comp):
    bad = FakeCompSource("keepa", raises=True)
    good = FakeCompSource("terapeak", {
        (VALID_UPC, Condition.USED): make_comp(
            80.0, 20, Condition.USED, source="terapeak"),
    })
    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        VALID_UPC, bin_cost=10.0, condition=Condition.USED,
        sources=[bad, good], settings=settings, conn=conn,
    )
    # one source threw, the other supplied a comp -> still evaluated
    assert comps is not None
    assert comps.sources == ("terapeak",)
    assert "keepa_error" in verdict.reasons


def test_evaluate_battery_flag_surfaces_but_doesnt_force_skip(settings, make_comp):
    # High-margin item that should BUY even with the hazmat penalty.
    src = FakeCompSource("keepa", {
        (VALID_UPC, Condition.NEW): make_comp(
            200.0, 25, Condition.NEW, source="keepa"),
    })
    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        VALID_UPC, bin_cost=10.0, condition=Condition.NEW,
        weight_class=WeightClass.UNDER_1LB, lithium=True,
        sources=[src], settings=settings, conn=conn,
    )
    assert costs.hazmat_penalty == 22.5
    assert "battery: hazmat flag" in verdict.reasons
    # net still strong -> BUY despite the flag
    assert verdict.decision == "BUY"


def test_evaluate_cheap_battery_item_skips(settings, make_comp):
    # Cheap battery item: hazmat penalty wipes the margin -> SKIP.
    src = FakeCompSource("keepa", {
        (VALID_UPC, Condition.OPEN_BOX): make_comp(
            25.0, 25, Condition.OPEN_BOX, source="keepa"),
    })
    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        VALID_UPC, bin_cost=8.0, condition=Condition.OPEN_BOX,
        weight_class=WeightClass.UNDER_1LB, lithium=True,
        sources=[src], settings=settings, conn=conn,
    )
    assert verdict.decision == "SKIP"
    assert "battery: hazmat flag" in verdict.reasons


def test_evaluate_works_without_db(settings, make_comp):
    src = FakeCompSource("keepa", {
        (VALID_UPC, Condition.OPEN_BOX): make_comp(
            100.0, 15, Condition.OPEN_BOX, source="keepa"),
    })
    verdict, comps, costs, scan_id = evaluate(
        VALID_UPC, bin_cost=10.0, sources=[src], settings=settings, conn=None,
    )
    assert scan_id is None
    assert verdict.decision == "BUY"
