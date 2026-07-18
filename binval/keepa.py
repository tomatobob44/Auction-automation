"""Module 2 — KeepaSource (paid API, primary automated comp source).

This is the ONLY file that talks to a paid external API. Calls are isolated
behind an injectable `http_get` seam so the parser is fully fixture-testable
with no network and no key — and so the local agent (Clawd) can later drive it
with its own backoff/caching.

Semantics note (accepted by the operator): Keepa does not sell true "sold"
comps. It exposes price *history* per condition plus a `monthlySold` count when
Amazon reveals it. We use the 90-day average (`avg90`) at the matched condition
as the resale estimate, as-is. The Module 5 calibration loop is what corrects
this proxy over time.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

from .comps import NORMALIZED_TO_KEEPA_INDICES, CompSource
from .models import CompResult, Condition, Identifier, IdentifierType

log = logging.getLogger(__name__)

_KEEPA_ENDPOINT = "https://api.keepa.com/product"
_DOMAIN_US = 1

# A Keepa avg90 of -1 means "no data for this index".
_NO_DATA = -1

HttpGet = Callable[[str], bytes]


def _default_http_get(url: str) -> bytes:
    """Default transport: a thin requests GET with retry + dynamic backoff,
    mirroring the LEGO brickdynasty stockAPI pattern (5s for 429, 2s for 5xx)."""
    import requests

    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 429:
                time.sleep(5.0 * (attempt + 1))
            elif resp.status_code >= 500:
                time.sleep(2.0 * (attempt + 1))
            else:
                resp.raise_for_status()
            last_exc = RuntimeError(f"keepa HTTP {resp.status_code}")
        except Exception as e:  # noqa: BLE001 — retried below
            last_exc = e
            time.sleep(2.0 * (attempt + 1))
    raise last_exc or RuntimeError("keepa request failed")


class _ResponseCache:
    """Tiny on-disk JSON cache keyed by request URL, with a TTL. Keeps personal
    loop query volume modest so paid token usage stays low."""

    def __init__(self, cache_dir: str = ".cache/keepa", ttl_seconds: int = 86_400):
        self._dir = Path(cache_dir)
        self._ttl = ttl_seconds

    def _path(self, key: str) -> Path:
        import hashlib
        h = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self._dir / f"{h}.json"

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self._ttl:
            return None
        return p.read_bytes()

    def put(self, key: str, data: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_bytes(data)


class KeepaSource(CompSource):
    name = "keepa"
    domain = "amazon"   # Keepa reports Amazon-side prices

    def __init__(self, api_key: str, http_get: HttpGet | None = None,
                 cache: _ResponseCache | None = None,
                 enable_text_search: bool = False):
        self._key = api_key
        self._http_get = http_get or _default_http_get
        self._cache = cache if cache is not None else _ResponseCache()
        # Free-text search uses the token-costly /search endpoint; off by
        # default. Free-text items go through the manual Terapeak path instead.
        self._enable_text_search = enable_text_search

    def _build_url(self, identifier: Identifier) -> str | None:
        base = f"{_KEEPA_ENDPOINT}?key={self._key}&domain={_DOMAIN_US}&stats=90"
        if identifier.type in (IdentifierType.UPC, IdentifierType.EAN):
            return f"{base}&code={identifier.value}"
        if identifier.type == IdentifierType.ASIN:
            return f"{base}&asin={identifier.value}"
        # Free-text: no clean lookup without /search; defer to manual path.
        return None

    def _fetch(self, url: str) -> dict | None:
        cached = self._cache.get(url)
        if cached is not None:
            raw = cached
        else:
            raw = self._http_get(url)
            self._cache.put(url, raw)
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def get_comps(self, identifier: Identifier,
                  condition: Condition) -> CompResult | None:
        indices = NORMALIZED_TO_KEEPA_INDICES.get(condition)
        if not indices:
            return None  # e.g. PARTS — Keepa has no clean series.

        url = self._build_url(identifier)
        if url is None:
            return None

        try:
            data = self._fetch(url)
        except Exception as e:  # network failure — let engine degrade, not crash
            log.warning("keepa fetch failed: %s", e)
            return None
        if not data:
            return None

        products = data.get("products") or []
        if not products:
            return None
        product = products[0]
        stats = product.get("stats") or {}
        avg90 = stats.get("avg90")
        if not isinstance(avg90, list):
            return None

        # Walk the index preference order; first index with data wins. Anything
        # past the first is a fallback (flagged), and the USED index is coarse.
        chosen_idx = None
        derived = False
        for rank, idx in enumerate(indices):
            if idx < len(avg90) and avg90[idx] not in (None, _NO_DATA):
                chosen_idx = idx
                derived = rank > 0
                break
        if chosen_idx is None:
            return None  # no data at this condition -> SKIP, don't substitute.

        # Keepa prices are in integer cents.
        median = avg90[chosen_idx] / 100.0
        # Only the blended USED series is coarse. A derived comp (condition
        # fallback) carries its own confidence penalty; charging both made the
        # open-box fallback mathematically unable to ever clear the gate.
        coarse = condition == Condition.USED

        # monthlySold is often absent on long-tail items. Absent means demand
        # is UNKNOWN, not zero — the confidence model treats those differently.
        monthly_sold = product.get("monthlySold")
        if isinstance(monthly_sold, (int, float)) and monthly_sold > 0:
            sold_count, count_known = int(monthly_sold), True
        else:
            sold_count, count_known = 0, False

        return CompResult(
            median_sold_price=round(median, 2),
            sold_count=sold_count,
            price_low=round(median * 0.9, 2),
            price_high=round(median * 1.1, 2),
            condition=condition,
            source_name=self.name,
            coarse_match=coarse,
            derived=derived,
            sold_count_known=count_known,
            domain=self.domain,
        )
