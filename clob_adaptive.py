from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Derived directly from the complete V15.2 execution log (7,152 signals):
# median / P90 bid-ask spreads by regime were approximately:
# CHEAP 1c / 5c, MID 1c / 3c, CORE 1c / 1c, HIGH 1c / 1c.
# We allow at least two ticks so a one-tick quote can be taken despite a
# single-tick move between book read and submission; the regime P90 is the cap.
REGIME_MAX_SPREAD = {
    "CHEAP": 0.05,
    "MID": 0.03,
    "CORE": 0.02,
    "HIGH": 0.02,
}

# The exchange minimum is a market constraint, not a strategy target.  This
# limits how much extra *batch* capital we will add solely to reach min shares.
# Small signals are first accumulated, so these are not per-signal multipliers.
REGIME_MAX_MINIMUM_UPLIFT = {
    "CHEAP": 3.00,
    "MID": 2.00,
    "CORE": 1.50,
    "HIGH": 1.25,
}

DEFAULT_BATCH_WINDOW_SECONDS = 6.0
DEFAULT_MAX_ORDER = 5.0


@dataclass(frozen=True)
class AdaptivePlan:
    items: Tuple[Dict[str, Any], ...]
    execution_price: float
    requested_budget: float
    order_shares: float
    min_order_cost: float
    min_shares: float
    topup: float
    max_execution_price: float


class CLOBAdaptivePlanner:
    """Plan a bounded-aggression BUY against the current Polymarket CLOB.

    The strategy signal keeps its original price/notional/regime.  Execution may
    pay the *current* ask only when that ask is within the signal's regime-derived
    price ceiling. Multiple signals may share one execution lot when every signal
    accepts that same current price. The exchange minimum can add a bounded
    top-up, but only after compatible signal budgets are accumulated.
    """

    def __init__(
        self,
        *,
        max_order: float = DEFAULT_MAX_ORDER,
        batch_window_seconds: float = DEFAULT_BATCH_WINDOW_SECONDS,
        regime_max_spread: Optional[Dict[str, float]] = None,
        regime_max_uplift: Optional[Dict[str, float]] = None,
    ):
        self.max_order = float(max_order)
        self.batch_window_seconds = max(0.0, float(batch_window_seconds))
        self.max_spread = dict(regime_max_spread or REGIME_MAX_SPREAD)
        self.max_uplift = dict(regime_max_uplift or REGIME_MAX_MINIMUM_UPLIFT)

    @staticmethod
    def max_price(signal_price: float, tick_size: float, regime: str) -> float:
        p = float(signal_price)
        tick = max(float(tick_size), 1e-6)
        spread_cap = max(float(REGIME_MAX_SPREAD.get(str(regime), 0.02)), 2.0 * tick)
        return min(0.999999, p + spread_cap)

    @staticmethod
    def _item_max_price(item: Dict[str, Any], tick_size: float) -> float:
        meta = item.get("meta") or {}
        explicit = meta.get("max_execution_price")
        if explicit is not None:
            return float(explicit)
        regime = str(meta.get("regime") or "MID")
        return CLOBAdaptivePlanner.max_price(float(item.get("price", 0.0)), tick_size, regime)

    def plan(
        self,
        items: Iterable[Dict[str, Any]],
        *,
        current_ask: float,
        min_shares: float,
        tick_size: float,
        now: float,
    ) -> Optional[AdaptivePlan]:
        if current_ask is None:
            return None
        try:
            ask = float(current_ask)
            min_s = float(min_shares)
        except (TypeError, ValueError):
            return None
        if not (0.0 < ask < 1.0) or min_s <= 0:
            return None

        candidates: List[Dict[str, Any]] = []
        for item in items:
            if str(item.get("status", "queued")) != "queued":
                continue
            created = float(item.get("created_at", now))
            if now - created > self.batch_window_seconds and candidates:
                # Old signals may still execute by themselves, but they should
                # not make a new batch wait for later unrelated signals.
                continue
            expiry = item.get("expires_at")
            if expiry is not None and now >= float(expiry):
                continue
            try:
                pmax = self._item_max_price(item, tick_size)
                amount = float(item.get("notional", 0.0))
            except (TypeError, ValueError):
                continue
            if amount <= 0 or pmax <= 0:
                continue
            if ask <= pmax + 1e-9:
                candidates.append(item)

        if not candidates:
            return None

        candidates.sort(key=lambda x: float(x.get("created_at", now)))
        minimum_cost = ask * min_s
        selected: List[Dict[str, Any]] = []
        intended = 0.0
        max_allowed_uplift = float("inf")
        max_pmax = ask

        for item in candidates:
            amount = max(0.0, float(item.get("notional", 0.0)))
            if amount <= 0:
                continue
            if intended + amount > self.max_order + 1e-9:
                continue
            selected.append(item)
            intended += amount
            regime = str((item.get("meta") or {}).get("regime") or "MID")
            max_allowed_uplift = min(
                max_allowed_uplift,
                float(self.max_uplift.get(regime, self.max_uplift.get("MID", 2.0))),
            )
            max_pmax = min(max_pmax, self._item_max_price(item, tick_size))
            if intended >= minimum_cost:
                break
            # As soon as the aggregate can be made exchange-valid with a bounded
            # minimum top-up, stop collecting more unrelated signals.
            if intended > 0 and minimum_cost / intended <= max_allowed_uplift:
                break

        if not selected or intended <= 0:
            return None

        # We cannot submit above the least-permissive signal's price ceiling.
        execution_price = min(ask, max_pmax)
        if execution_price + 1e-9 < ask:
            return None

        minimum_cost = execution_price * min_s
        if intended < minimum_cost:
            uplift = minimum_cost / intended
            if uplift > max_allowed_uplift + 1e-9:
                # Try to add another signal that still accepts this price.
                for item in candidates:
                    if item in selected:
                        continue
                    amount = max(0.0, float(item.get("notional", 0.0)))
                    if amount <= 0 or intended + amount > self.max_order + 1e-9:
                        continue
                    selected.append(item)
                    intended += amount
                    regime = str((item.get("meta") or {}).get("regime") or "MID")
                    max_allowed_uplift = min(
                        max_allowed_uplift,
                        float(self.max_uplift.get(regime, self.max_uplift.get("MID", 2.0))),
                    )
                    if minimum_cost / intended <= max_allowed_uplift + 1e-9:
                        break

        if intended <= 0:
            return None
        minimum_cost = execution_price * min_s
        if minimum_cost > self.max_order + 1e-9:
            return None

        if intended < minimum_cost:
            uplift = minimum_cost / intended
            if uplift > max_allowed_uplift + 1e-9:
                return None
            budget = minimum_cost
        else:
            budget = min(intended, self.max_order)

        # Limit shares so the worst-case cost at the limit price stays within the
        # chosen budget. A FAK/marketable limit may fill cheaper than this price.
        shares = budget / execution_price
        if shares + 1e-9 < min_s:
            return None

        return AdaptivePlan(
            items=tuple(selected),
            execution_price=execution_price,
            requested_budget=budget,
            order_shares=shares,
            min_order_cost=minimum_cost,
            min_shares=min_s,
            topup=max(0.0, budget - intended),
            max_execution_price=max_pmax,
        )
