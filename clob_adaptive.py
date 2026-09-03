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
    """Execute current available liquidity without changing V15.2 allocations.

    A signal keeps its original strategy fine band and dollar allocation. The
    executor may pay the current best ask (and consume deeper asks up to that
    band's upper boundary), but it never moves the signal into another band and
    never invents extra capital to satisfy the exchange minimum.
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
    def _band_bounds(fine_band: str) -> Optional[Tuple[float, float]]:
        band = str(fine_band or "").strip()
        if len(band) < 4:
            return None
        try:
            # V15.2 fine-band names are e.g. C20_30, M50_60, R80_90, H95_100.
            lo_s, hi_s = band[1:].split("_", 1)
            lo = float(lo_s) / 100.0
            hi = float(hi_s) / 100.0
            return lo, hi
        except (TypeError, ValueError):
            return None

    @classmethod
    def same_fine_band(cls, fine_band: str, price: float) -> bool:
        bounds = cls._band_bounds(fine_band)
        if bounds is None:
            return False
        lo, hi = bounds
        p = float(price)
        if str(fine_band).endswith("_100") and p == 1.0:
            return True
        return lo <= p < hi

    @classmethod
    def max_price(cls, signal_price: float, tick_size: float, regime: str, fine_band: Optional[str] = None) -> float:
        """Return the maximum executable price without leaving the strategy band.

        The strategy's fine band is the execution boundary.  We deliberately do
        not add an arbitrary spread/uplift allowance here: the current ask may be
        taken wherever it is available *inside the same V15.2 band*.
        """
        p = float(signal_price)
        tick = max(float(tick_size), 1e-6)
        if fine_band:
            bounds = cls._band_bounds(fine_band)
            if bounds is not None:
                _lo, hi = bounds
                # Keep a one-tick numerical margin below the next band.
                return min(0.999999, max(p, hi - tick if hi < 1.0 else 0.999999))
        # Fallback for old persisted queue entries that lack fine_band metadata:
        # preserve the conservative legacy cap.
        spread_cap = max(float(REGIME_MAX_SPREAD.get(str(regime), 0.02)), 2.0 * tick)
        return min(0.999999, p + spread_cap)

    @classmethod
    def _item_max_price(cls, item: Dict[str, Any], tick_size: float) -> float:
        meta = item.get("meta") or {}
        explicit = meta.get("max_execution_price")
        if explicit is not None:
            return float(explicit)
        regime = str(meta.get("regime") or "MID")
        fine_band = meta.get("fine_band")
        return cls.max_price(float(item.get("price", 0.0)), tick_size, regime, fine_band)

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
                continue
            expiry = item.get("expires_at")
            if expiry is not None and now >= float(expiry):
                continue
            meta = item.get("meta") or {}
            fine_band = str(meta.get("fine_band") or "")
            amount = max(0.0, float(item.get("notional", 0.0)))
            if amount <= 0:
                continue
            # Execution may use whatever liquidity is currently offered, but
            # never outside the signal's original V15.2 fine price band.
            if fine_band:
                if not self.same_fine_band(fine_band, ask):
                    continue
            else:
                pmax = self._item_max_price(item, tick_size)
                if ask > pmax + 1e-9:
                    continue
            candidates.append(item)

        if not candidates:
            return None

        candidates.sort(key=lambda x: float(x.get("created_at", now)))
        # A single CLOB order is allowed to represent only one strategy fine
        # band. This keeps the measured per-band capital/trade distribution intact.
        selected_band = str((candidates[0].get("meta") or {}).get("fine_band") or "")
        if selected_band:
            candidates = [x for x in candidates if str((x.get("meta") or {}).get("fine_band") or "") == selected_band]
        if not candidates:
            return None

        minimum_cost = ask * min_s
        selected: List[Dict[str, Any]] = []
        intended = 0.0
        for item in candidates:
            amount = max(0.0, float(item.get("notional", 0.0)))
            if amount <= 0:
                continue
            if intended + amount > self.max_order + 1e-9:
                continue
            selected.append(item)
            intended += amount
            if intended + 1e-12 >= minimum_cost:
                break

        # No synthetic capital top-up: every dollar in the submitted order must
        # originate from a real V15.2 signal allocation. If the exchange minimum
        # cannot be met by compatible signals, wait for another signal.
        if not selected or intended + 1e-12 < minimum_cost:
            return None
        if intended > self.max_order + 1e-9:
            return None

        shares = intended / ask
        if shares + 1e-9 < min_s:
            return None

        return AdaptivePlan(
            items=tuple(selected),
            execution_price=ask,
            requested_budget=intended,
            order_shares=shares,
            min_order_cost=minimum_cost,
            min_shares=min_s,
            topup=0.0,
            max_execution_price=ask,
        )

