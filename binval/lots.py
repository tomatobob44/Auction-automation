"""Module 6 — Inventory-finding agent (COMPLIANT online sourcing).

Watches ONLY sanctioned liquidation marketplaces (B-Stock, Direct Liquidation,
BULQ, ...) whose business model is selling to buyers like you — never sites
defending against bots. Runs each lot's items through Modules 1-4 and surfaces
only lots scoring above the BUY floor.

Phase 1 ships the framework + a file/fixture-driven LotSource so the loop is
fully testable. A live B-Stock adapter slots in behind the same interface once
buyer-account feed access exists. No source has an open public sold-data API,
so we never parse hostile HTML here.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from abc import ABC, abstractmethod

from .comps import CompSource
from .config import Settings
from .engine import evaluate
from .models import Condition, WeightClass
from .verdict import format_terse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LotItem:
    identifier_raw: str
    qty: int = 1
    condition: Condition = Condition.OPEN_BOX
    title: str | None = None
    weight_class: WeightClass = WeightClass.LB_1_3
    lithium: bool = False


@dataclass(frozen=True)
class Lot:
    lot_id: str
    source: str
    lot_cost: float
    items: list[LotItem]
    url: str | None = None


@dataclass
class LotReport:
    lot_id: str
    source: str
    url: str | None
    lot_cost: float
    total_net: float
    item_count: int
    buy_items: int
    surfaced: bool
    lines: list[str] = field(default_factory=list)


class LotSource(ABC):
    name: str

    @abstractmethod
    def fetch_lots(self) -> list[Lot]:
        raise NotImplementedError


class FileLotSource(LotSource):
    """Reads a JSON manifest — the same shape a live B-Stock buyer-feed adapter
    will emit. Lets the whole Module 6 loop run and be tested offline."""

    name = "file"

    def __init__(self, path: Path):
        self._path = Path(path)

    def fetch_lots(self) -> list[Lot]:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        lots = []
        for L in raw.get("lots", []):
            items = [
                LotItem(
                    identifier_raw=str(it["identifier"]),
                    qty=int(it.get("qty", 1)),
                    condition=Condition(it.get("condition", Condition.OPEN_BOX.value)),
                    title=it.get("title"),
                    weight_class=WeightClass(it.get("weight", WeightClass.LB_1_3.value)),
                    lithium=bool(it.get("lithium", False)),
                )
                for it in L.get("items", [])
            ]
            lots.append(Lot(
                lot_id=str(L["lot_id"]),
                source=str(L.get("source", self.name)),
                lot_cost=float(L["lot_cost"]),
                items=items,
                url=L.get("url"),
            ))
        return lots


def evaluate_lot(
    lot: Lot,
    *,
    sources: list[CompSource],
    settings: Settings,
    conn: sqlite3.Connection | None = None,
) -> LotReport:
    """Value every item in a lot and decide whether the lot clears the floor.

    Per-item bin cost is the lot cost split evenly across all units (simple and
    conservative; a comp-value-weighted allocation is a future refinement). A
    lot surfaces when its summed estimated net clears `thresholds.lot_buy_floor`.
    """
    total_units = sum(it.qty for it in lot.items) or 1
    per_unit_cost = lot.lot_cost / total_units

    total_net = 0.0
    buy_items = 0
    lines: list[str] = []

    for it in lot.items:
        verdict, comps, costs, scan_id = evaluate(
            it.identifier_raw,
            bin_cost=per_unit_cost,
            condition=it.condition,
            weight_class=it.weight_class,
            lithium=it.lithium,
            sources=sources,
            settings=settings,
            conn=conn,
            source_store=f"{lot.source}:{lot.lot_id}",
        )
        # A lot's economics are driven by what you can actually sell; count
        # net only from items that individually clear the BUY bar.
        if verdict.decision == "BUY":
            buy_items += 1
            total_net += verdict.net * it.qty
        label = it.title or it.identifier_raw
        lines.append(f"{label[:32]:<32} x{it.qty} {format_terse(verdict, comps, scan_id)}")

    total_net = round(total_net, 2)
    surfaced = total_net >= settings.thresholds.lot_buy_floor
    return LotReport(
        lot_id=lot.lot_id,
        source=lot.source,
        url=lot.url,
        lot_cost=lot.lot_cost,
        total_net=total_net,
        item_count=len(lot.items),
        buy_items=buy_items,
        surfaced=surfaced,
        lines=lines,
    )


def polite_get(
    url: str,
    *,
    cache_dir: str = ".cache/lots",
    min_interval_s: float = 2.0,
    user_agent: str = "binval/0.1 (solo resale sourcing tool; prospective buyer)",
    ttl_seconds: int = 3600,
) -> bytes:
    """Compliant HTTP GET for any future live LotSource: honest user agent,
    on-disk caching, and a minimum interval between requests. You are a
    prospective buyer checking listings — the intended use — not an adversary
    evading defenses. If a source's ToS forbids automated access, do not call
    this; check it manually instead.
    """
    import hashlib

    import requests

    cdir = Path(cache_dir)
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    cached = cdir / f"{key}.bin"
    if cached.exists() and time.time() - cached.stat().st_mtime < ttl_seconds:
        return cached.read_bytes()

    time.sleep(min_interval_s)
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    cdir.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(resp.content)
    return resp.content
