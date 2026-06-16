"""Log -> outcome update -> calibration aggregate flow against a temp sqlite."""
from __future__ import annotations

import pytest

from binval.config import _DEFAULT_COSTS
from binval.costs import landed_net
from binval.db import calibration_report, connect, log_scan, record_outcome
from binval.models import (
    CombinedComps,
    Condition,
    Identifier,
    IdentifierType,
    Verdict,
    WeightClass,
)


def _seed_scan(conn, *, net=41.25, verdict_decision="BUY"):
    ident = Identifier(IdentifierType.UPC, "036000291452", confidence=1.0)
    comps = CombinedComps(
        median=100.0, sold_count=12, condition=Condition.OPEN_BOX,
        sources=("keepa", "terapeak"), price_low=90.0, price_high=110.0,
    )
    costs = landed_net(
        100.0, 20.0, marketplace="ebay", category="default",
        weight_class=WeightClass.LB_1_3, condition=Condition.OPEN_BOX,
        tables=_DEFAULT_COSTS,
    )
    verdict = Verdict(decision=verdict_decision, net=costs.net,
                      margin_multiple=2.06, confidence=0.82)
    return log_scan(
        conn, identifier=ident, condition=Condition.OPEN_BOX,
        category="default", marketplace="ebay", comps=comps, costs=costs,
        verdict=verdict, source_store="Cincinnati Bin Co",
    )


def test_log_scan_returns_id_and_persists(settings):
    conn = connect(settings.db_path)
    scan_id = _seed_scan(conn)
    assert scan_id >= 1
    row = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
    assert row["identifier"] == "036000291452"
    assert row["comp_source"] == "keepa,terapeak"
    assert row["verdict"] == "BUY"
    assert row["actual_net"] is None  # not yet recorded


def test_record_outcome_clean_sale(settings):
    conn = connect(settings.db_path)
    scan_id = _seed_scan(conn)
    # sold for 95, shipping 11 actual
    actual = record_outcome(
        conn, scan_id, sold_price=95.0, ship_cost=11.0,
        tables=_DEFAULT_COSTS, days_to_sell=12,
    )
    # fee 95*0.13=12.35, pay 95*0.03=2.85, net = 95-20-12.35-2.85-11 = 48.80
    assert actual == 48.80
    row = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
    assert row["actual_net"] == 48.80
    assert row["was_returned"] == 0
    assert row["days_to_sell"] == 12


def test_record_outcome_returned_is_full_loss(settings):
    conn = connect(settings.db_path)
    scan_id = _seed_scan(conn)
    actual = record_outcome(
        conn, scan_id, sold_price=95.0, ship_cost=11.0,
        tables=_DEFAULT_COSTS, was_returned=True, return_reason="INAD",
    )
    # full loss = -(bin 20 + ship 11) = -31.0
    assert actual == -31.0
    row = conn.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
    assert row["was_returned"] == 1
    assert row["return_reason"] == "INAD"


def test_record_outcome_unknown_scan_raises(settings):
    conn = connect(settings.db_path)
    with pytest.raises(KeyError):
        record_outcome(conn, 999, sold_price=10.0, ship_cost=1.0,
                       tables=_DEFAULT_COSTS)


def test_calibration_report_aggregates(settings):
    conn = connect(settings.db_path)
    # two clean sales + one return, all OPEN_BOX/default
    s1 = _seed_scan(conn)
    s2 = _seed_scan(conn)
    s3 = _seed_scan(conn)
    record_outcome(conn, s1, sold_price=95.0, ship_cost=11.0, tables=_DEFAULT_COSTS)
    record_outcome(conn, s2, sold_price=95.0, ship_cost=11.0, tables=_DEFAULT_COSTS)
    record_outcome(conn, s3, sold_price=95.0, ship_cost=11.0,
                   tables=_DEFAULT_COSTS, was_returned=True, return_reason="DOA")

    report = calibration_report(conn)
    assert len(report) == 1
    row = report[0]
    assert row.condition == Condition.OPEN_BOX.value
    assert row.category == "default"
    assert row.n == 3
    # 1 of 3 returned
    assert row.measured_return_rate == pytest.approx(0.333, abs=1e-3)
    assert row.avg_est_net is not None
    assert row.avg_actual_net is not None


def test_calibration_ignores_unrecorded(settings):
    conn = connect(settings.db_path)
    _seed_scan(conn)  # no outcome recorded
    assert calibration_report(conn) == []
