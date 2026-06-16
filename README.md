# binval — single-item liquidation resale valuation engine

Scan or type an item identifier (UPC / ASIN / title), pull resale comps from
**sanctioned sources only**, subtract the *true landed cost* — including a
defect/INAD allowance for reselling returns — and get a terse **BUY / SKIP**
verdict you can read standing in a bin-store aisle. Every scan and sale outcome
is logged to SQLite so your estimates calibrate against reality over time.

**Design principle:** the valuation *logic* is deterministic math in code, not
in a model. Modules 1, 3, 4, 5 (identify, landed cost, verdict, logging) are
fully local — no API, no model calls — so the daily scan loop can later run on a
free local agent. Module 2 (comp lookup) is the only external dependency and is
isolated behind a `CompSource` interface.

## Install

```bash
pip install -e ".[dev]"      # Python 3.11+
cp .env.example .env         # then fill in KEEPA_API_KEY (optional)
```

`zxing-cpp` ships self-contained wheels — no system packages (no `libzbar0`)
needed for barcode decoding.

## Configuration

- **Cost tables & thresholds:** `config/costs.toml` — fees, shipping (biased
  high), hazmat/oversize penalties, condition-scaled defect rates, and the
  BUY thresholds. Edit freely; no code changes needed. Hardcoded defaults
  mirror the file, so the engine runs even with no config present.
- **Secrets:** `KEEPA_API_KEY` (and optional `EBAY_OAUTH_TOKEN`) are read from
  the environment / a git-ignored `.env`. Without a Keepa key the engine still
  runs on manual Terapeak entry.
- **Database:** `BINVAL_DB` (default `./binval.db`).

## Comp sources (sanctioned only — never scrape eBay)

| Source | Status | Notes |
|---|---|---|
| **Keepa** | primary, automated | paid API; uses the 90-day avg price at the matched condition. The engine is fully functional on Keepa alone. |
| **Terapeak (manual)** | available now | you pull median sold + count from Seller Hub by hand and pass `--manual-median/--manual-count`. Zero ToS risk. |
| **eBay Marketplace Insights** | dormant stub | real sold data, but approval-gated. Condition-ID mapping is wired; the live path activates when a token is present. |

Condition is normalized into one enum (`NEW, OPEN_BOX, REFURB, USED, PARTS`).
Comps are only ever compared like-for-like; an unmatched comp is dropped, never
blended. Two sources diverging >40% at the same condition → take the
conservative lower value. No clean comp → **SKIP** (don't guess). Default item
condition is **OPEN_BOX** unless clearly sealed — bin stock is mostly returns.

## Usage

```bash
# Value an item (Keepa auto-used if KEEPA_API_KEY set; add a manual comp too)
binval eval 036000291452 --bin-cost 5 --condition open_box \
    --manual-median 39.99 --manual-count 12

# From a barcode photo
binval eval --image photo.jpg --bin-cost 5 --condition open_box

# Flags: --weight {under_1lb,lb_1_3,lb_3_10,over_10lb}  --lithium  --oversize
#        --marketplace {ebay,amazon}  --category electronics  --store "Bin Co"

# Record a real sale outcome against a scan id (calibrates the defect rate)
binval outcome 1 --sold 38.50 --ship 6.00 --days 9
binval outcome 1 --sold 38.50 --ship 6.00 --returned --reason INAD

# Estimate-vs-actual accuracy + measured return rate per condition/category
binval calibrate

# Module 6: run a liquidation lot manifest, surface lots above the buy floor
binval lots tests/fixtures/lots_sample.json --manual-median 80 --manual-count 20

# Phone web UI (defaults to 127.0.0.1; use --host 0.0.0.0 for phone-on-LAN)
binval serve --host 0.0.0.0 --port 8000
```

Sample verdict line:

```
✅ BUY  net +$13.84   4.6x  conf 0.81 | 12 sold @ ~$39.99 [terapeak] #3
❌ SKIP net -$19.95   -2.5x conf 0.90 | net<$10, margin<3x, battery: hazmat flag #2
```

## The calibration loop (the moat)

Every scan is logged. After a sale, `binval outcome` records what actually
happened — including `was_returned` / `return_reason`. `binval calibrate` then
compares estimated vs actual net and shows your *measured* return rate per
condition/category. When those diverge from `config/costs.toml [defect_rates]`,
edit the table to match. Your engine gets sharper per item processed.

## Module 6 — compliant online sourcing

The inventory agent watches **only sanctioned liquidation marketplaces**
(B-Stock, Direct Liquidation, BULQ, …) whose business model is selling to
buyers — never sites defending against bots. Phase 1 ships the `LotSource`
framework plus a file/fixture-driven source (`FileLotSource`) so the loop is
testable today; a live buyer-feed adapter slots in behind the same interface.
The `polite_get()` helper enforces honest user agent + caching + rate limits.
**Never** build a bot-detection-evading scraper or point this at retailer sites
that block bots.

## Tests

```bash
pytest -q
```

Highest-value coverage is the pure math (`test_costs.py`, `test_verdict.py`) and
the condition mapping/combine logic (`test_comps.py`). The Keepa client is
tested against a fixture via an injected `http_get` — **no network, no API key
required**. End-to-end engine, db, lots, and web tests all run with a
`FakeCompSource`.

## Security note

The web UI has no authentication and is intended for use from your own phone on
a trusted LAN. It binds to `127.0.0.1` by default; only pass `--host 0.0.0.0`
on a network you control.
