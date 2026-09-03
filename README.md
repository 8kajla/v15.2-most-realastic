# V15.2 — Exact Trader Distribution + Realistic Fill Simulation

This build preserves the verified V15.2 strategy and trader-behavior data unchanged and adds a research-grade resting-maker fill simulation for PAPER mode.

## Exact strategy preservation

- `strategy.py` is unchanged from the verified V15.2 reference.
- `trader_behavior.json` is unchanged from the verified V15.2 reference.
- Live/shadow strategy mechanics continue to use the $300 virtual strategy frame.
- Live dollar sizing defaults to the verified 1/3 scale for the $100 execution envelope.

## Paper-mode dual simulation

With `PAPER_TRADING=true`, the bot runs two ledgers from the exact same strategy signal stream:

- `paper_state.json` — legacy instant-fill benchmark. This is retained so the historical/all-signal result remains measurable.
- `realistic_fill_state.json` — realistic resting-maker result. A signal becomes `PENDING`; cash and positions are unchanged until a qualifying public CLOB trade print advances its simulated queue and creates an actual fill.

The same signal is therefore compared side-by-side without changing the strategy because of whether the realistic order filled.

## Realistic fill mechanics

The simulator uses Polymarket's public market WebSocket `last_trade_price` events. These events provide the trade price, size, aggressive side, and timestamp.

For a resting BUY:

1. Capture displayed bid depth at the target price at placement.
2. Require a subsequent `SELL` trade print at or below the target price.
3. Accumulate qualifying trade volume only after the order was placed and before market end.
4. Treat displayed depth plus earlier simulated same-price orders as an estimated queue-ahead amount.
5. Book partial fills immediately using the observed trade price.
6. Keep remaining shares pending until another qualifying print or expiry.
7. At the market's 5-minute end, expire any remaining quantity as `EXPIRED_UNFILLED`.

Queue position is an approximation. It is not an assertion of exact exchange priority, hidden liquidity, or market impact.

## Research outputs

- `realistic_fill_orders.json` — persistent simulated resting orders and queue state.
- `realistic_orders.csv` — every realistic order placement.
- `realistic_fills.csv` — every simulated fill increment.
- `realistic_unfilled.csv` — every expired/unfilled order.
- `realistic_settlement_details.csv` — filled-only settlement outcomes and P&L.
- `realistic_fill_metrics_1min.csv` — instant vs realistic P&L plus fill/expiry counts and rates.
- `realistic_fill_regime_1min.csv` — fill/expiry rates and latency by CHEAP/MID/CORE/HIGH.
- `realistic_fill_band_1min.csv` — fill/expiry rates and latency by fine price band.

The regime/band metrics include average, P50, and P90 fill latency.

## Crash recovery

Simulator state is persisted before the main loop consumes fill events. On restart, the bot reconciles simulator-recorded filled shares/cost against `realistic_fill_state.json` and books any missing fill delta at its weighted-average persisted fill price.

## Shadow and live modes

- `SHADOW_CLOB=true` remains public-data-only and never submits authenticated orders.
- `LIVE_TRADING=true` remains the authenticated CLOB path with the existing safety/risk controls.
- The realistic resting-maker paper simulator is intentionally a PAPER-mode research mechanism and does not replace authenticated live execution.

## Useful environment variables

- `REALISTIC_FILL_SIM=true` — enable the dual-ledger realistic paper simulation (default in PAPER mode).
- `MARKET_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `MARKET_WS_RECONNECT_SECONDS=2`
- `LIVE_SIZE_SCALE=0.3333333333333333`
- `HARD_CUTOFF_SECONDS=60`

Run `pytest -q` before deployment.
