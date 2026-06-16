"""Shared test fixtures: default cost tables, a fake CompSource, and an
in-memory/temp SQLite connection. No network, no API keys required.
"""
from __future__ import annotations

import pytest

from binval.comps import CompSource
from binval.config import _DEFAULT_COSTS, _DEFAULT_THRESHOLDS, Settings
from binval.models import CompResult, Condition, Identifier


@pytest.fixture
def tables():
    return _DEFAULT_COSTS


@pytest.fixture
def thresholds():
    return _DEFAULT_THRESHOLDS


@pytest.fixture
def settings(tmp_path):
    return Settings(
        costs=_DEFAULT_COSTS,
        thresholds=_DEFAULT_THRESHOLDS,
        keepa_api_key=None,
        ebay_oauth_token=None,
        db_path=str(tmp_path / "test.db"),
    )


class FakeCompSource(CompSource):
    """Returns canned CompResults keyed by (identifier_value, condition).

    Construct with a dict {(value, Condition): CompResult | None}. A missing
    key returns None (the "no clean comp" path). Pass raises=True to simulate
    a source that throws (network/auth failure) on every call.
    """

    def __init__(self, name: str, canned: dict | None = None, raises: bool = False):
        self.name = name
        self._canned = canned or {}
        self._raises = raises

    def get_comps(self, identifier: Identifier, condition: Condition):
        if self._raises:
            raise RuntimeError(f"{self.name} simulated failure")
        return self._canned.get((identifier.value, condition))


@pytest.fixture
def make_comp():
    """Factory for quick CompResult construction in tests."""
    def _make(median, count, condition, *, low=None, high=None,
              source="fake", coarse=False, derived=False):
        return CompResult(
            median_sold_price=median,
            sold_count=count,
            price_low=low if low is not None else median * 0.9,
            price_high=high if high is not None else median * 1.1,
            condition=condition,
            source_name=source,
            coarse_match=coarse,
            derived=derived,
        )
    return _make
