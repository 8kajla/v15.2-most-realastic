# V15.2 CLOB-Adaptive Research Basis

This package preserves strategy.py and trader_behavior.json unchanged from the exact V15.2 reference. The changes are execution-only.

## Source facts
- 7,152 V15.2 paper signals in the provided log.
- Total paper signal notional: $9,118.63.
- Largest individual paper signal: $13.06.
- Reconstructed maximum simultaneous paper cost from trade/resolution timestamps: $185.94; at 1/3 live scaling: about $61.98.
- Reconstructed maximum single-market paper cost: $92.03; at 1/3 scaling: about $30.68.
- No individual 1/3-scaled signal in the log reaches a 5-share minimum at its logged bid, so standalone exact-size submission cannot cover the historical signal stream.

## Trader distribution
The unchanged trader_behavior.json reports:

- CHEAP: 59.31% of trades; 10.93% of notional.
- MID: 24.21% of trades; 15.53% of notional.
- CORE: 9.30% of trades; 16.43% of notional.
- HIGH: 7.17% of trades; 57.11% of notional.

## Observed CLOB context in the supplied V15.2 log

- MID: 3843 signals; mean notional $0.920; median spread $0.010; P90 spread $0.030; median logged bid depth 106.00.
- CHEAP: 2147 signals; mean notional $0.231; median spread $0.010; P90 spread $0.050; median logged bid depth 70.00.
- HIGH: 373 signals; mean notional $8.691; median spread $0.010; P90 spread $0.010; median logged bid depth 179.67.
- CORE: 789 signals; mean notional $2.336; median spread $0.010; P90 spread $0.010; median logged bid depth 165.00.

## Adaptive execution policy
- CHEAP maximum price drift: 5¢ (P90 observed spread).
- MID maximum price drift: 3¢ (P90 observed spread).
- CORE maximum price drift: 2¢ (1¢ observed P90 plus one-tick safety floor).
- HIGH maximum price drift: 2¢ (1¢ observed P90 plus one-tick safety floor).
- Minimum-order top-up is applied only at the aggregate execution-lot level, not by rewriting an individual signal.
- Default batch window: 6 seconds, close to the trader process median 2-second cadence while remaining well inside the 60-second cutoff.
- Execution uses a marketable FAK limit at the current ask; it does not rest at a stale historical bid.
- Risk limits remain $100 total, $5 single order, $33.3333 per market, 20 open orders, $10 daily loss.

## Important limitation
This is an evidence-constrained CLOB-aware execution model, not proof of the reference trader's hidden execution logic. Exact exchange queue priority cannot be reconstructed from the supplied log alone.
