# V15.2 CLOB Shadow Mode

This package runs the V15.2 strategy with the $300 virtual strategy frame and a $100 live-style execution budget, but **never submits an authenticated Polymarket order**.

## What is real

- Polymarket market discovery
- Polymarket public CLOB order books
- Market minimum order size
- Market tick size
- Best bid / best ask
- Public last-trade price and side
- V15.2 strategy selection, cadence, price, side, and 60-second cutoff
- The same durable execution queue and $100 risk/reservation model used by the live package
- GTC/post-only semantics are validated locally before creating a synthetic order

## What is simulated

- Authentication/signing
- Actual order submission
- Exact exchange queue position
- Exact fill quantity when only public REST last-trade data is available

A synthetic resting BUY is marked filled when a subsequent public last-trade observation reports a SELL at or below the synthetic order price. The fill is booked at the synthetic order price and is explicitly logged as SHADOW.

This is intentionally more conservative about strategy timing than the old paper ledger because a signal does not become a position merely because V15.2 generated it.

## Run locally

```bash
cp railway.env.example .env
export $(grep -v '^#' .env | xargs)
python bot.py
```

For a clean run set `FRESH_START=true`.

## Railway

Use the included Dockerfile. Set:

```text
PAPER_TRADING=false
LIVE_TRADING=false
SHADOW_CLOB=true
STARTING_CAPITAL=300
LIVE_BANKROLL_CAP=100
LIVE_SIZE_SCALE=0.3333333333333333
MAX_TOTAL_EXPOSURE=100
MAX_SINGLE_ORDER=5
MAX_MARKET_EXPOSURE=33.3333333333
MAX_OPEN_ORDERS=20
MAX_DAILY_LOSS=10
FRESH_START=true
DATA_DIR=/app/data
LOOP_SECONDS=1
ORDERBOOK_SAMPLE_SECONDS=1
DECISION_SAMPLE_SECONDS=1
HARD_CUTOFF_SECONDS=60
MIN_TRADE_GAP_SECONDS=0
REPORT_INTERVAL_SECONDS=30
```

No private key or Polymarket trading credentials are required. Do not add them to this shadow service.

Railway's current available deployment regions include Singapore but not Mumbai. Polymarket's current geographic restrictions list Singapore as close-only, so Railway Singapore must be treated as **shadow/public-data only** and not as a live-ordering workaround.
