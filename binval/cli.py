"""Command-line interface.

Subcommands:
  eval       value a single item (UPC/ASIN/title or --image), print BUY/SKIP
  outcome    record a real sale result against a scan_id
  calibrate  print est-vs-actual accuracy + measured return rates
  lots       run a lot manifest through the engine, surface above-floor lots
  serve      launch the phone web UI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .comps import CompSource, ManualCompSource
from .config import load_settings
from .db import calibration_report, connect, record_outcome
from .engine import evaluate
from .models import Condition, WeightClass
from .verdict import format_terse


def _build_sources(settings, args) -> list[CompSource]:
    """Assemble the comp sources for this run from config + CLI flags.

    - Keepa is added when a key exists (the automated backbone).
    - A manual Terapeak entry is added when --manual-median is supplied.
    """
    sources: list[CompSource] = []
    if settings.keepa_api_key:
        from .keepa import KeepaSource
        sources.append(KeepaSource(settings.keepa_api_key))
    if getattr(args, "manual_median", None) is not None:
        sources.append(ManualCompSource(
            median=args.manual_median,
            count=args.manual_count or 0,
            condition=Condition(args.condition),
        ))
    return sources


def _cmd_eval(args) -> int:
    settings = load_settings(args.config)
    sources = _build_sources(settings, args)
    if not sources:
        print("No comp sources available. Set KEEPA_API_KEY or pass "
              "--manual-median/--manual-count (Terapeak).", file=sys.stderr)
        return 2

    if args.image:
        raw: str | bytes = Path(args.image).read_bytes()
    else:
        raw = args.identifier

    conn = connect(settings.db_path)
    verdict, comps, costs, scan_id = evaluate(
        raw,
        bin_cost=args.bin_cost,
        condition=Condition(args.condition),
        weight_class=WeightClass(args.weight),
        marketplace=args.marketplace,
        category=args.category,
        lithium=args.lithium,
        oversize=args.oversize,
        sources=sources,
        settings=settings,
        conn=conn,
        source_store=args.store,
    )
    print(format_terse(verdict, comps, scan_id))
    return 0


def _cmd_outcome(args) -> int:
    settings = load_settings(args.config)
    conn = connect(settings.db_path)
    actual = record_outcome(
        conn, args.scan_id,
        sold_price=args.sold,
        ship_cost=args.ship,
        tables=settings.costs,
        was_returned=args.returned,
        return_reason=args.reason,
        days_to_sell=args.days,
    )
    print(f"#{args.scan_id} actual_net ${actual:+.2f}"
          + ("  [RETURNED]" if args.returned else ""))
    return 0


def _cmd_calibrate(args) -> int:
    settings = load_settings(args.config)
    conn = connect(settings.db_path)
    rows = calibration_report(conn)
    if not rows:
        print("No recorded outcomes yet. Use `binval outcome` after sales.")
        return 0
    hdr = f"{'condition':<10} {'category':<12} {'n':>3} {'est':>8} {'actual':>8} {'err':>8} {'ret%':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r.condition:<10} {r.category:<12} {r.n:>3} "
              f"{(r.avg_est_net or 0):>8.2f} {(r.avg_actual_net or 0):>8.2f} "
              f"{(r.avg_error or 0):>8.2f} "
              f"{(r.measured_return_rate or 0) * 100:>5.1f}%")
    # Show the configured defect rates for comparison.
    print("\nConfigured defect_rates (edit config/costs.toml to match measured):")
    for cond, rate in settings.costs.defect_rates.items():
        print(f"  {cond.value:<10} {rate * 100:.1f}%")
    return 0


def _cmd_lots(args) -> int:
    settings = load_settings(args.config)
    sources = _build_sources(settings, args)
    from .lots import FileLotSource, evaluate_lot

    conn = connect(settings.db_path)
    source = FileLotSource(Path(args.manifest))
    surfaced = 0
    for lot in source.fetch_lots():
        report = evaluate_lot(lot, sources=sources, settings=settings, conn=conn)
        if report.surfaced:
            surfaced += 1
            print(f"\n*** LOT {report.lot_id} [{report.source}] "
                  f"est net ${report.total_net:+.2f} on ${report.lot_cost:.2f} "
                  f"({report.buy_items}/{report.item_count} BUY items) ***")
            if report.url:
                print(f"    {report.url}")
            for line in report.lines:
                print("    " + line)
    if surfaced == 0:
        print(f"No lots cleared the buy floor "
              f"(${settings.thresholds.lot_buy_floor:.2f}).")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="binval",
                                description="Liquidation resale valuation engine")
    p.add_argument("--config", default=None, help="path to costs.toml")
    sub = p.add_subparsers(dest="command", required=True)

    # eval
    e = sub.add_parser("eval", help="value a single item")
    e.add_argument("identifier", nargs="?", default="",
                   help="UPC / ASIN / free-text title")
    e.add_argument("--image", help="path to a barcode image instead of identifier")
    e.add_argument("--bin-cost", type=float, required=True, dest="bin_cost")
    e.add_argument("--condition", default=Condition.OPEN_BOX.value,
                   choices=[c.value for c in Condition])
    e.add_argument("--weight", default=WeightClass.LB_1_3.value,
                   choices=[w.value for w in WeightClass])
    e.add_argument("--marketplace", default="ebay")
    e.add_argument("--category", default="default")
    e.add_argument("--lithium", action="store_true", help="lithium battery item")
    e.add_argument("--oversize", action="store_true")
    e.add_argument("--store", default=None, help="source store label for the log")
    e.add_argument("--manual-median", type=float, default=None,
                   dest="manual_median", help="Terapeak median sold price")
    e.add_argument("--manual-count", type=int, default=None,
                   dest="manual_count", help="Terapeak sold count")
    e.set_defaults(func=_cmd_eval)

    # outcome
    o = sub.add_parser("outcome", help="record a sale outcome")
    o.add_argument("scan_id", type=int)
    o.add_argument("--sold", type=float, required=True, help="actual sold price")
    o.add_argument("--ship", type=float, required=True, help="actual shipping cost")
    o.add_argument("--returned", action="store_true")
    o.add_argument("--reason", default=None, help="return reason (e.g. INAD, DOA)")
    o.add_argument("--days", type=int, default=None, help="days to sell")
    o.set_defaults(func=_cmd_outcome)

    # calibrate
    c = sub.add_parser("calibrate", help="est-vs-actual + measured return rates")
    c.set_defaults(func=_cmd_calibrate)

    # lots
    l = sub.add_parser("lots", help="evaluate a lot manifest (Module 6)")
    l.add_argument("manifest", help="path to a JSON lot manifest")
    l.add_argument("--manual-median", type=float, default=None, dest="manual_median")
    l.add_argument("--manual-count", type=int, default=None, dest="manual_count")
    l.add_argument("--condition", default=Condition.OPEN_BOX.value,
                   choices=[c.value for c in Condition])
    l.set_defaults(func=_cmd_lots)

    # serve
    s = sub.add_parser("serve", help="launch the phone web UI")
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (use 0.0.0.0 for phone-on-LAN)")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=_cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
