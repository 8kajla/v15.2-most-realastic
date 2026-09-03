# V15.2 Realistic Fill Simulation

This build preserves the verified `strategy.py` and `trader_behavior.json` unchanged.
Paper mode runs two ledgers from the same strategy decisions:

1. `paper_state.json` = legacy instant-fill benchmark. Every strategy signal is immediately booked so historical signal/P&L behavior remains available.
2. `realistic_fill_state.json` = resting-maker simulation. A signal creates a `PENDING` shadow order and does not affect cash/positions until qualifying public CLOB trade-tape volume reaches the simulated queue.

The realistic simulator consumes Polymarket's unauthenticated market WebSocket `last_trade_price` events. A BUY resting order is fillable only on a qualifying SELL print at or below its target price, and the event includes price, size, side, and timestamp. Queue position is approximated using displayed bid depth at placement plus earlier simulated orders at the same token/price. This is intentionally an approximation, not exact exchange queue priority.

Orders expire at the 5-minute market end if they have not fully filled. Partial fills are booked immediately to the realistic ledger; remaining size stays pending until another qualifying print or expiry.

Outputs:

- `realistic_fill_orders.json`: persistent order state.
- `realistic_orders.csv`: every resting signal/order placement.
- `realistic_fills.csv`: every simulated fill and settlement-side record.
- `realistic_unfilled.csv`: every expired/canceled unfilled order.
- `realistic_fill_metrics_1min.csv`: overall fill/expiry rates, deployed/fill notional, and side-by-side instant vs realistic P&L.
- `realistic_state.json` is not used; the ledger is `realistic_fill_state.json`.

Environment:

- `REALISTIC_FILL_SIM=true` (default in paper mode)
- `MARKET_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market`
- `MARKET_WS_RECONNECT_SECONDS=2`

The realistic simulation is a research model, not a guarantee of actual exchange fills. It does not capture hidden liquidity, exact queue priority, or market impact from our own resting order.
