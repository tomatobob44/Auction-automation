"""Configuration loading: cost tables (costs.toml) + secrets (.env / env).

Hardcoded defaults mirror config/costs.toml so the engine runs with no
config file present; TOML values override the defaults field-by-field.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .models import Condition, WeightClass

# Load .env from the current working directory / nearest parent once on import.
load_dotenv()

_DEFAULT_CONFIG_PATH = Path("config/costs.toml")


@dataclass(frozen=True)
class CostTables:
    # marketplace -> {category -> fee fraction}
    marketplace_fees: dict[str, dict[str, float]]
    payment_rate: float
    shipping: dict[WeightClass, float]
    materials: float
    lithium_penalty: float
    oversize_penalty: float
    defect_rates: dict[Condition, float]
    # Cross-marketplace realization: Amazon-side comp prices (Keepa) scaled to
    # expected eBay realized prices when selling on eBay.
    amazon_to_ebay: float = 0.75

    def marketplace_fee_rate(self, marketplace: str, category: str) -> float:
        table = self.marketplace_fees.get(marketplace.lower(), {})
        return table.get(category, table.get("default", 0.0))


@dataclass(frozen=True)
class Thresholds:
    min_net: float = 10.0
    min_margin_multiple: float = 3.0
    min_confidence: float = 0.5
    lot_buy_floor: float = 100.0
    # ((max_bin_cost, required_multiple), ...) — first tier whose max_bin_cost
    # >= bin_cost wins. Empty tuple -> flat min_margin_multiple.
    margin_tiers: tuple[tuple[float, float], ...] = ()

    def required_margin(self, bin_cost: float) -> float:
        for max_cost, multiple in self.margin_tiers:
            if bin_cost <= max_cost:
                return multiple
        return self.min_margin_multiple


@dataclass(frozen=True)
class Settings:
    costs: CostTables
    thresholds: Thresholds
    keepa_api_key: str | None = None
    ebay_oauth_token: str | None = None
    db_path: str = "binval.db"


# --- Hardcoded defaults (mirror config/costs.toml) -------------------------

_DEFAULT_COSTS = CostTables(
    marketplace_fees={
        "ebay": {"default": 0.13},
        "amazon": {"default": 0.15, "electronics": 0.08},
    },
    payment_rate=0.03,
    shipping={
        WeightClass.UNDER_1LB: 6.00,
        WeightClass.LB_1_3: 10.00,
        WeightClass.LB_3_10: 16.00,
        WeightClass.OVER_10LB: 28.00,
    },
    materials=0.75,
    lithium_penalty=22.50,
    oversize_penalty=15.00,
    defect_rates={
        Condition.NEW: 0.025,
        Condition.OPEN_BOX: 0.10,
        Condition.REFURB: 0.125,
        Condition.USED: 0.15,
        Condition.PARTS: 0.0,
    },
)

_DEFAULT_THRESHOLDS = Thresholds()


def _parse_costs(raw: dict) -> CostTables:
    """Build CostTables from parsed TOML, falling back to defaults per field."""
    fees_raw = raw.get("fees", {})
    payment_rate = float(fees_raw.get("payment_rate", _DEFAULT_COSTS.payment_rate))

    marketplace_fees: dict[str, dict[str, float]] = {}
    for mkt in ("ebay", "amazon"):
        table = fees_raw.get(mkt)
        if isinstance(table, dict):
            marketplace_fees[mkt] = {k: float(v) for k, v in table.items()}
        else:
            marketplace_fees[mkt] = dict(_DEFAULT_COSTS.marketplace_fees[mkt])

    ship_raw = raw.get("shipping", {})
    shipping = dict(_DEFAULT_COSTS.shipping)
    for wc in WeightClass:
        if wc.value in ship_raw:
            shipping[wc] = float(ship_raw[wc.value])
    materials = float(ship_raw.get("materials", _DEFAULT_COSTS.materials))

    pen_raw = raw.get("penalties", {})
    lithium = float(pen_raw.get("lithium", _DEFAULT_COSTS.lithium_penalty))
    oversize = float(pen_raw.get("oversize", _DEFAULT_COSTS.oversize_penalty))

    defect_raw = raw.get("defect_rates", {})
    defect_rates = dict(_DEFAULT_COSTS.defect_rates)
    for cond in Condition:
        # TOML keys are uppercase (NEW, OPEN_BOX, ...)
        if cond.name in defect_raw:
            defect_rates[cond] = float(defect_raw[cond.name])

    realization_raw = raw.get("realization", {})
    amazon_to_ebay = float(realization_raw.get(
        "amazon_to_ebay", _DEFAULT_COSTS.amazon_to_ebay))

    return CostTables(
        marketplace_fees=marketplace_fees,
        payment_rate=payment_rate,
        shipping=shipping,
        materials=materials,
        lithium_penalty=lithium,
        oversize_penalty=oversize,
        defect_rates=defect_rates,
        amazon_to_ebay=amazon_to_ebay,
    )


def _parse_thresholds(raw: dict) -> Thresholds:
    t = raw.get("thresholds", {})
    d = _DEFAULT_THRESHOLDS
    tiers_raw = t.get("margin_tiers", [])
    margin_tiers = tuple(
        (float(pair[0]), float(pair[1]))
        for pair in tiers_raw
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    )
    return Thresholds(
        min_net=float(t.get("min_net", d.min_net)),
        min_margin_multiple=float(t.get("min_margin_multiple", d.min_margin_multiple)),
        min_confidence=float(t.get("min_confidence", d.min_confidence)),
        lot_buy_floor=float(t.get("lot_buy_floor", d.lot_buy_floor)),
        margin_tiers=margin_tiers,
    )


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    """Load cost tables + thresholds from TOML and secrets from the environment.

    config_path resolution: explicit arg → $BINVAL_CONFIG → config/costs.toml.
    A missing file is fine; hardcoded defaults are used.
    """
    path = Path(config_path or os.getenv("BINVAL_CONFIG") or _DEFAULT_CONFIG_PATH)
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        costs = _parse_costs(raw)
        thresholds = _parse_thresholds(raw)
    else:
        costs = _DEFAULT_COSTS
        thresholds = _DEFAULT_THRESHOLDS

    return Settings(
        costs=costs,
        thresholds=thresholds,
        keepa_api_key=os.getenv("KEEPA_API_KEY") or None,
        ebay_oauth_token=os.getenv("EBAY_OAUTH_TOKEN") or None,
        db_path=os.getenv("BINVAL_DB") or "binval.db",
    )
