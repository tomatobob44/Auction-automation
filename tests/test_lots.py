"""Module 6: FileLotSource parsing + evaluate_lot surfacing with FakeCompSource."""
from __future__ import annotations

from pathlib import Path

import pytest

from binval.db import connect
from binval.lots import FileLotSource, evaluate_lot
from binval.models import Condition, WeightClass
from tests.conftest import FakeCompSource

FIXTURE = Path(__file__).parent / "fixtures" / "lots_sample.json"
UPC = "036000291452"
ASIN = "B07FZ8S74R"


def test_file_lot_source_parses_manifest():
    lots = FileLotSource(FIXTURE).fetch_lots()
    assert len(lots) == 2
    lot = lots[0]
    assert lot.lot_id == "BS-1001"
    assert lot.source == "b-stock"
    assert lot.lot_cost == 60.0
    assert len(lot.items) == 2
    assert lot.items[0].qty == 3
    assert lot.items[0].condition == Condition.OPEN_BOX
    assert lot.items[1].weight_class == WeightClass.UNDER_1LB


def test_evaluate_lot_surfaces_above_floor(settings):
    # Strong comps on both items -> lot clears the $100 floor.
    src = FakeCompSource("keepa", {
        (UPC, Condition.OPEN_BOX): _comp(80.0, 20, Condition.OPEN_BOX),
        (ASIN, Condition.NEW): _comp(150.0, 25, Condition.NEW),
    })
    conn = connect(settings.db_path)
    lots = FileLotSource(FIXTURE).fetch_lots()
    report = evaluate_lot(lots[0], sources=[src], settings=settings, conn=conn)
    assert report.surfaced is True
    assert report.buy_items >= 1
    assert report.total_net >= settings.thresholds.lot_buy_floor


def test_evaluate_lot_below_floor_not_surfaced(settings):
    # Expensive lot, weak single used comp -> stays below floor.
    src = FakeCompSource("keepa", {
        (UPC, Condition.USED): _comp(25.0, 5, Condition.USED),
    })
    conn = connect(settings.db_path)
    lots = FileLotSource(FIXTURE).fetch_lots()
    report = evaluate_lot(lots[1], sources=[src], settings=settings, conn=conn)
    assert report.surfaced is False


def test_evaluate_lot_logs_each_item(settings):
    src = FakeCompSource("keepa", {
        (UPC, Condition.OPEN_BOX): _comp(80.0, 20, Condition.OPEN_BOX),
        (ASIN, Condition.NEW): _comp(150.0, 25, Condition.NEW),
    })
    conn = connect(settings.db_path)
    lots = FileLotSource(FIXTURE).fetch_lots()
    evaluate_lot(lots[0], sources=[src], settings=settings, conn=conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM scans").fetchone()["c"]
    assert n == 2  # both items logged


# helper
def _comp(median, count, condition):
    from binval.models import CompResult
    return CompResult(
        median_sold_price=median, sold_count=count,
        price_low=median * 0.9, price_high=median * 1.1,
        condition=condition, source_name="keepa",
    )
