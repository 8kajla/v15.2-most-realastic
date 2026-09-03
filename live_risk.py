from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class LiveRisk:
    """Persistent, fail-closed risk engine for the $100 live deployment.

    The V15.2 strategy remains calibrated to its $300 virtual frame. These
    limits only govern real-money execution and may be lowered by environment
    configuration, never raised above the hard live ceilings below.
    """

    HARD_BANKROLL_CAP = 100.00
    HARD_MAX_TOTAL = 100.00
    HARD_MAX_ORDER = 15.00
    HARD_MAX_MARKET = 33.3333333333
    HARD_MAX_OPEN_ORDERS = 20
    HARD_DAILY_LOSS = 10.00

    def __init__(self, state_path=None):
        self.bankroll_cap = min(
            float(os.getenv("LIVE_BANKROLL_CAP", "100")),
            self.HARD_BANKROLL_CAP,
        )
        self.max_total = min(
            float(os.getenv("MAX_TOTAL_EXPOSURE", "100")),
            self.HARD_MAX_TOTAL,
            self.bankroll_cap,
        )
        self.max_order = min(
            float(os.getenv("MAX_SINGLE_ORDER", "15")),
            self.HARD_MAX_ORDER,
        )
        self.max_market = min(
            float(os.getenv("MAX_MARKET_EXPOSURE", "33.3333333333")),
            self.HARD_MAX_MARKET,
        )
        self.max_orders = min(
            int(os.getenv("MAX_OPEN_ORDERS", "20")),
            self.HARD_MAX_OPEN_ORDERS,
        )
        self.daily_loss = min(
            float(os.getenv("MAX_DAILY_LOSS", "10")),
            self.HARD_DAILY_LOSS,
        )
        self.state_path = Path(state_path) if state_path else None
        self.day = datetime.now(timezone.utc).date().isoformat()
        self.day_start_realized = 0.0
        self.realized_total = 0.0
        self.realized_today = 0.0
        self.halted = False
        self.halt_reason = ""
        self._load()

    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text())
            if d.get("day") == self.day:
                self.day_start_realized = float(d.get("day_start_realized", 0.0))
                self.realized_total = float(d.get("realized_total", 0.0))
                self.realized_today = float(d.get("realized_today", 0.0))
                self.halted = bool(d.get("halted", False))
                self.halt_reason = str(d.get("halt_reason", ""))
        except Exception:
            self.halted = True
            self.halt_reason = "CORRUPT_RISK_STATE"

    def _save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "day": self.day,
            "day_start_realized": self.day_start_realized,
            "realized_total": self.realized_total,
            "realized_today": self.realized_today,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }, indent=2))
        os.replace(tmp, self.state_path)

    def sync_realized(self, total_realized):
        total = float(total_realized)
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.day:
            self.day = today
            self.day_start_realized = total
            self.realized_today = 0.0
            self.halted = False
            self.halt_reason = ""
        self.realized_total = total
        self.realized_today = total - self.day_start_realized
        if self.realized_today <= -self.daily_loss:
            self.halted = True
            self.halt_reason = "DAILY_LOSS_CAP"
        self._save()

    def record_realized(self, pnl):
        self.realized_total += float(pnl)
        self.realized_today += float(pnl)
        if self.realized_today <= -self.daily_loss:
            self.halted = True
            self.halt_reason = "DAILY_LOSS_CAP"
        self._save()

    def halt(self, reason):
        self.halted = True
        self.halt_reason = str(reason)
        self._save()

    def authorize(self, notional, total_exposure, market_exposure, open_orders, cash, reserved=0.0):
        n = float(notional)
        total = float(total_exposure)
        market = float(market_exposure)
        reserve = float(reserved)
        available = float(cash) - reserve
        if self.halted:
            return False, f"risk-halted:{self.halt_reason}"
        if n <= 0:
            return False, "non-positive-order"
        if n > self.bankroll_cap + 1e-9:
            return False, "bankroll-cap"
        if total + n > self.bankroll_cap + 1e-9:
            return False, "bankroll-cap"
        if n > self.max_order + 1e-9:
            return False, "single-order-cap"
        if total + n > self.max_total + 1e-9:
            return False, "total-exposure-cap"
        if market + n > self.max_market + 1e-9:
            return False, "market-exposure-cap"
        if int(open_orders) >= self.max_orders:
            return False, "open-order-cap"
        if available + 1e-9 < n:
            return False, "insufficient-live-cash"
        if self.realized_today <= -self.daily_loss:
            self.halt("DAILY_LOSS_CAP")
            return False, "daily-loss-cap"
        return True, "ok"
