"""FastAPI app via TestClient, with FakeCompSource injected (no API key)."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from binval.config import _DEFAULT_COSTS, _DEFAULT_THRESHOLDS, Settings
from binval.models import Condition
from binval.web import create_app
from tests.conftest import FakeCompSource

UPC = "036000291452"


def _comp(median, count, condition):
    from binval.models import CompResult
    return CompResult(
        median_sold_price=median, sold_count=count,
        price_low=median * 0.9, price_high=median * 1.1,
        condition=condition, source_name="keepa",
    )


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        costs=_DEFAULT_COSTS, thresholds=_DEFAULT_THRESHOLDS,
        keepa_api_key=None, ebay_oauth_token=None,
        db_path=str(tmp_path / "web.db"),
    )

    def factory(_settings, condition, m_median, m_count):
        return [FakeCompSource("keepa", {
            (UPC, Condition.OPEN_BOX): _comp(100.0, 15, Condition.OPEN_BOX),
        })]

    return TestClient(create_app(settings=settings, sources_factory=factory))


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "binval" in r.text
    assert "VALUE IT" in r.text


def test_eval_returns_buy_verdict(client):
    r = client.post("/eval", data={
        "identifier": UPC, "bin_cost": "10", "condition": "open_box",
        "weight": "lb_1_3", "marketplace": "ebay",
    })
    assert r.status_code == 200
    assert "BUY" in r.text
    assert "verdict buy" in r.text


def test_eval_photo_upload_decodes(client):
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image

    try:
        if hasattr(zxingcpp, "create_barcode"):
            bc = zxingcpp.create_barcode(UPC, zxingcpp.BarcodeFormat.UPCA)
            img = zxingcpp.write_barcode_to_image(bc)
        else:
            img = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.UPCA, UPC)
    except Exception:
        pytest.skip("no barcode writer")
    if not isinstance(img, Image.Image):
        img = Image.fromarray(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    r = client.post(
        "/eval",
        data={"bin_cost": "10", "condition": "open_box", "weight": "lb_1_3",
              "marketplace": "ebay"},
        files={"image": ("barcode.png", buf, "image/png")},
    )
    assert r.status_code == 200
    assert "BUY" in r.text


def test_outcome_records_and_shows(client):
    # first create a scan
    client.post("/eval", data={
        "identifier": UPC, "bin_cost": "10", "condition": "open_box",
        "weight": "lb_1_3", "marketplace": "ebay",
    })
    r = client.post("/outcome", data={
        "scan_id": "1", "sold": "95", "ship": "11",
    })
    assert r.status_code == 200
    assert "actual_net" in r.text
