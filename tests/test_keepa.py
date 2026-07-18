"""KeepaSource parser tests, fed fixture JSON via an injected http_get.
Zero network, zero API key — required since this env has no Keepa key."""
from __future__ import annotations

from pathlib import Path

import pytest

from binval.keepa import KeepaSource, _ResponseCache
from binval.models import Condition, Identifier, IdentifierType

FIXTURE = (Path(__file__).parent / "fixtures" / "keepa_product.json").read_bytes()


class _NoCache(_ResponseCache):
    """Cache that never stores or returns — forces the http_get path."""
    def __init__(self):
        pass

    def get(self, key):
        return None

    def put(self, key, data):
        pass


def _source(payload: bytes = FIXTURE):
    calls = {"n": 0}

    def fake_get(url: str) -> bytes:
        calls["n"] += 1
        assert "key=TESTKEY" in url
        return payload

    src = KeepaSource("TESTKEY", http_get=fake_get, cache=_NoCache())
    return src, calls


def _upc():
    return Identifier(IdentifierType.UPC, "036000291452", confidence=1.0)


def test_keepa_new_condition():
    src, _ = _source()
    r = src.get_comps(_upc(), Condition.NEW)
    assert r is not None
    # index 1 = 3999 cents = 39.99
    assert r.median_sold_price == 39.99
    assert r.condition == Condition.NEW
    assert r.sold_count == 14          # monthlySold
    assert r.coarse_match is False
    assert r.derived is False


def test_keepa_used_is_coarse():
    src, _ = _source()
    r = src.get_comps(_upc(), Condition.USED)
    assert r is not None
    # index 2 = 2999 cents
    assert r.median_sold_price == 29.99
    assert r.coarse_match is True       # Keepa "Used" blends subconditions


def test_keepa_refurb():
    src, _ = _source()
    r = src.get_comps(_upc(), Condition.REFURB)
    assert r is not None
    # index 6 = 3499 cents
    assert r.median_sold_price == 34.99


def test_keepa_open_box_falls_back_to_used_like_new():
    # Warehouse (idx 9) is -1; falls back to Used-Like-New (idx 19 = 3299)
    src, _ = _source()
    r = src.get_comps(_upc(), Condition.OPEN_BOX)
    assert r is not None
    assert r.median_sold_price == 32.99
    assert r.derived is True            # came from a fallback index
    # derived carries ONLY the derived penalty — charging coarse as well made
    # the fallback mathematically unable to ever clear the confidence gate.
    assert r.coarse_match is False


def test_keepa_parts_returns_none():
    src, _ = _source()
    assert src.get_comps(_upc(), Condition.PARTS) is None


def test_keepa_freetext_returns_none_by_default():
    src, _ = _source()
    text = Identifier(IdentifierType.TEXT, "some title", confidence=0.6)
    assert src.get_comps(text, Condition.NEW) is None


def test_keepa_no_products_returns_none():
    src, _ = _source(payload=b'{"products": []}')
    assert src.get_comps(_upc(), Condition.NEW) is None


def test_keepa_all_nodata_at_condition_returns_none():
    # A product whose avg90 has no value at any OPEN_BOX index.
    payload = b'{"products":[{"stats":{"avg90":[100,200,300]}}]}'
    src, _ = _source(payload=payload)
    # OPEN_BOX indices are 9/19/20 — all out of range -> None
    assert src.get_comps(_upc(), Condition.OPEN_BOX) is None


def test_keepa_asin_builds_asin_url():
    captured = {}

    def fake_get(url: str) -> bytes:
        captured["url"] = url
        return FIXTURE

    src = KeepaSource("TESTKEY", http_get=fake_get, cache=_NoCache())
    asin = Identifier(IdentifierType.ASIN, "B07FZ8S74R", confidence=1.0)
    src.get_comps(asin, Condition.NEW)
    assert "asin=B07FZ8S74R" in captured["url"]
    assert "code=" not in captured["url"]
