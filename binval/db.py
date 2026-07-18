"""Module 5 — Log everything (the moat + the future content).

Every scan, BUY or SKIP, and every eventual sale outcome writes to a local
SQLite store. Comparing est_net vs actual_net tunes thresholds over time;
was_returned / return_reason calibrate the defect allowance from a guess into
your real measured return rate per condition/category.

Pure stdlib sqlite3 — fully local, no server, runnable by the local agent.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import CostTables
from .costs import marketplace_fee, payment_fee
from .models import CombinedComps, Condition, CostBreakdown, Identifier, Verdict

_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS scans (
    scan_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT    NOT NULL DEFAULT (datetime('now')),
    source_store         TEXT,
    identifier_type      TEXT    NOT NULL,
    identifier           TEXT    NOT NULL,
    title                TEXT,
    condition            TEXT    NOT NULL,
    category             TEXT,
    marketplace          TEXT,
    comp_median          REAL,
    comp_count           INTEGER,
    comp_source          TEXT,
    bin_cost             REAL    NOT NULL,
    est_shipping         REAL,
    est_defect_allowance REAL,
    est_net              REAL,
    verdict              TEXT    NOT NULL,
    confidence           REAL,
    -- outcome columns, NULL until recorded
    actual_sold_price    REAL,
    actual_ship_cost     REAL,
    was_returned         INTEGER,
    return_reason        TEXT,
    actual_net           REAL,
    days_to_sell         INTEGER,
    labor_minutes        REAL
);
CREATE INDEX IF NOT EXISTS idx_scans_identifier ON scans(identifier);
CREATE INDEX IF NOT EXISTS idx_scans_condition  ON scans(condition);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the scans database with the schema applied.
    Existing databases from older versions are migrated in place."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add columns introduced after the first release."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(scans)")}
    if "labor_minutes" not in existing:
        conn.execute("ALTER TABLE scans ADD COLUMN labor_minutes REAL")


def log_scan(
    conn: sqlite3.Connection,
    *,
    identifier: Identifier,
    condition: Condition,
    category: str,
    marketplace: str,
    comps: CombinedComps | None,
    costs: CostBreakdown,
    verdict: Verdict,
    source_store: str | None = None,
) -> int:
    """Insert one scan row. Returns the new scan_id."""
    cur = conn.execute(
        """
        INSERT INTO scans (
            source_store, identifier_type, identifier, title, condition,
            category, marketplace, comp_median, comp_count, comp_source,
            bin_cost, est_shipping, est_defect_allowance, est_net,
            verdict, confidence
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_store,
            identifier.type.value,
            identifier.value,
            identifier.raw_title,
            condition.value,
            category,
            marketplace,
            comps.median if comps else None,
            comps.sold_count if comps else None,
            ",".join(comps.sources) if comps else None,
            costs.bin_cost,
            costs.shipping,
            costs.defect_allowance,
            costs.net,
            verdict.decision,
            verdict.confidence,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_outcome(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    sold_price: float,
    ship_cost: float,
    tables: CostTables,
    was_returned: bool = False,
    return_reason: str | None = None,
    days_to_sell: int | None = None,
    labor_minutes: float | None = None,
) -> float:
    """Record the real sale result for a scan and compute actual_net.

    actual_net = sold_price - bin_cost - marketplace_fee - payment_fee
                 - ship_cost   (materials/defect were estimates; the real
                                 shipping + return status are what we measure)
    A returned item nets a full loss: -(bin_cost + ship_cost) — you ate the
    item and the outbound shipping (and typically return shipping too).
    """
    row = conn.execute(
        "SELECT bin_cost, marketplace, category FROM scans WHERE scan_id=?",
        (scan_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"scan_id {scan_id} not found")

    bin_cost = row["bin_cost"]
    marketplace = row["marketplace"] or "ebay"
    category = row["category"] or "default"

    if was_returned:
        # Lost the item cost and the outbound shipping; refunded the sale.
        actual_net = round(-(bin_cost + ship_cost), 2)
    else:
        fee = marketplace_fee(sold_price, marketplace, category, tables)
        pay = payment_fee(sold_price, marketplace, tables)
        actual_net = round(sold_price - bin_cost - fee - pay - ship_cost, 2)

    conn.execute(
        """
        UPDATE scans SET
            actual_sold_price=?, actual_ship_cost=?, was_returned=?,
            return_reason=?, actual_net=?, days_to_sell=?,
            labor_minutes=COALESCE(?, labor_minutes)
        WHERE scan_id=?
        """,
        (
            sold_price,
            ship_cost,
            1 if was_returned else 0,
            return_reason,
            actual_net,
            days_to_sell,
            labor_minutes,
            scan_id,
        ),
    )
    conn.commit()
    return actual_net


@dataclass
class CalibrationRow:
    condition: str
    category: str
    n: int
    avg_est_net: float | None
    avg_actual_net: float | None
    avg_error: float | None
    measured_return_rate: float | None


def calibration_report(conn: sqlite3.Connection) -> list[CalibrationRow]:
    """Per condition/category: estimate accuracy and measured return rate.

    Only rows with a recorded outcome (actual_net IS NOT NULL) contribute.
    The measured return rate is the calibration feedback for [defect_rates].
    """
    rows = conn.execute(
        """
        SELECT
            condition,
            COALESCE(category, 'default') AS category,
            COUNT(*)                                       AS n,
            AVG(est_net)                                   AS avg_est_net,
            AVG(actual_net)                                AS avg_actual_net,
            AVG(actual_net - est_net)                      AS avg_error,
            AVG(CASE WHEN was_returned=1 THEN 1.0 ELSE 0.0 END) AS return_rate
        FROM scans
        WHERE actual_net IS NOT NULL
        GROUP BY condition, COALESCE(category, 'default')
        ORDER BY condition, category
        """
    ).fetchall()

    def _r(v):
        return round(v, 2) if v is not None else None

    return [
        CalibrationRow(
            condition=r["condition"],
            category=r["category"],
            n=r["n"],
            avg_est_net=_r(r["avg_est_net"]),
            avg_actual_net=_r(r["avg_actual_net"]),
            avg_error=_r(r["avg_error"]),
            measured_return_rate=(round(r["return_rate"], 3)
                                  if r["return_rate"] is not None else None),
        )
        for r in rows
    ]


def labor_rate(conn: sqlite3.Connection) -> tuple[float, float, int] | None:
    """($/labor-hour, total_hours, n) over outcomes with labor recorded.

    This is THE number the field test exists to produce: whether the business
    beats your alternative hourly rate — $/item flatters, $/hour decides.
    """
    row = conn.execute(
        """
        SELECT SUM(actual_net) AS net, SUM(labor_minutes) AS mins,
               COUNT(*) AS n
        FROM scans
        WHERE actual_net IS NOT NULL AND labor_minutes IS NOT NULL
              AND labor_minutes > 0
        """
    ).fetchone()
    if not row or not row["mins"]:
        return None
    hours = row["mins"] / 60.0
    return round(row["net"] / hours, 2), round(hours, 2), row["n"]
