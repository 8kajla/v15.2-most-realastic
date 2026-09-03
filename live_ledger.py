from __future__ import annotations

import json
import os
import time
from pathlib import Path


TERMINAL_TRADE_STATUSES = {
    "TRADE_STATUS_CONFIRMED",
    "CONFIRMED",
    "TRADE_STATUS_FAILED",
    "FAILED",
    "TRADE_STATUS_CANCELLED",
    "CANCELLED",
    "CANCELED",
}
CONFIRMED_TRADE_STATUSES = {"TRADE_STATUS_CONFIRMED", "CONFIRMED"}
FAILED_TRADE_STATUSES = {
    "TRADE_STATUS_FAILED",
    "FAILED",
    "TRADE_STATUS_CANCELLED",
    "CANCELLED",
    "CANCELED",
}


class LiveLedger:
    """Durable live accounting with fail-closed execution reconciliation.

    Submitted orders are reservations only. Cash and positions change only when
    Polymarket reports the user's trade as CONFIRMED. MATCHED trades remain
    pending until they become terminal, so an asynchronous failed match cannot
    permanently corrupt the local ledger.
    """

    HARD_BANKROLL = 100.0
    HARD_ORDER = 15.0

    def __init__(self, path, initial_cash=100.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_cash = min(float(initial_cash), self.HARD_BANKROLL)
        self.cash = self.initial_cash
        self.realized = 0.0
        self.positions = {}
        self.orders = {}
        self.trades = []
        self.seen_trades = set()
        self.pending_trades = {}
        self._load()
        self._enforce_invariants()
        self.save()

    def _load(self):
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text())
        except Exception as exc:
            raise RuntimeError(f"LIVE LEDGER CORRUPT: cannot parse state: {exc}") from exc
        self.initial_cash = min(float(d.get("initial_cash", self.initial_cash)), self.HARD_BANKROLL)
        self.cash = float(d.get("cash", self.initial_cash))
        self.realized = float(d.get("realized", 0.0))
        self.positions = d.get("positions", {})
        self.orders = d.get("orders", {})
        self.trades = d.get("trades", [])
        self.seen_trades = set(d.get("seen_trades", []))
        self.pending_trades = d.get("pending_trades", {})

    def _enforce_invariants(self):
        if self.cash < -1e-8:
            raise RuntimeError(f"LIVE LEDGER CORRUPT: negative cash ${self.cash:.8f}")
        if self.initial_cash < 0 or self.initial_cash > self.HARD_BANKROLL + 1e-9:
            raise RuntimeError("LIVE LEDGER CORRUPT: invalid initial cash")
        if self.total_open_cost() > self.HARD_BANKROLL + 1e-6:
            raise RuntimeError("LIVE LEDGER CORRUPT: open exposure exceeds hard $100 cap")
        for oid, order in self.orders.items():
            reserved = float(order.get("reserved", 0.0))
            filled = float(order.get("filled_cost", 0.0))
            if reserved < -1e-9 or reserved > self.HARD_ORDER + 1e-6:
                raise RuntimeError(f"LIVE LEDGER CORRUPT: invalid order reservation {oid}")
            if filled < -1e-9 or filled > reserved + 1e-6:
                raise RuntimeError(f"LIVE LEDGER CORRUPT: invalid order fill {oid}")

    def save(self):
        self._enforce_invariants()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "realized": self.realized,
            "positions": self.positions,
            "orders": self.orders,
            "trades": self.trades[-20000:],
            "seen_trades": list(self.seen_trades)[-50000:],
            "pending_trades": self.pending_trades,
        }, indent=2))
        os.replace(tmp, self.path)

    def record_order(self, order_id, condition, token, side, price, notional, market, meta=None):
        oid = str(order_id)
        if not oid:
            raise ValueError("empty order id")
        n = float(notional)
        if n <= 0 or n > self.HARD_ORDER + 1e-9:
            raise ValueError("live order exceeds hard $5 order cap")
        existing = self.orders.get(oid)
        if existing:
            if str(existing.get("condition")) != str(condition) or str(existing.get("token")) != str(token):
                raise RuntimeError("LIVE RECONCILIATION HALT: order id reused for a different order")
            return
        self.orders[oid] = {
            "condition": condition,
            "token": str(token),
            "side": side,
            "price": float(price),
            "reserved": n,
            "filled_cost": 0.0,
            "market": market,
            "status": "SUBMITTED",
            "ts": time.time(),
            **(meta or {}),
        }
        self.save()

    @staticmethod
    def _maker_fills(trade, order_id):
        oid = str(order_id)
        fills = []
        for maker in trade.get("maker_orders") or []:
            maker_oid = str(maker.get("order_id") or maker.get("orderId") or "")
            if maker_oid != oid:
                continue
            amount = float(maker.get("matched_amount") or maker.get("matchedAmount") or 0)
            price = float(maker.get("price") or trade.get("price") or 0)
            if amount > 0 and 0 < price < 1:
                fills.append((amount, price, maker))
        return fills

    @staticmethod
    def _taker_fill(trade, order_id):
        oid = str(order_id)
        taker_oid = str(trade.get("taker_order_id") or trade.get("takerOrderId") or "")
        if taker_oid != oid:
            return None
        size = trade.get("size") or trade.get("matched_amount") or trade.get("matchedAmount") or trade.get("filled_size") or trade.get("filledSize")
        price = trade.get("price")
        try:
            shares = float(size)
            px = float(price)
        except (TypeError, ValueError):
            return None
        if shares <= 0 or not 0 < px < 1:
            return None
        return (shares, px, trade)

    @staticmethod
    def _status(trade):
        return str(trade.get("status", "")).upper().strip()

    def _fee_for_confirmed_fill(self, trade, maker):
        trader_side = str(trade.get("trader_side") or trade.get("traderSide") or "").upper()
        if trader_side == "MAKER" or maker is not None:
            raw_rate = trade.get("fee_rate_bps") or trade.get("feeRateBps")
            if raw_rate not in (None, "", "0", 0, 0.0):
                try:
                    if float(raw_rate) > 0:
                        raise RuntimeError("LIVE RECONCILIATION HALT: maker fill reports non-zero fee_rate_bps")
                except ValueError as exc:
                    raise RuntimeError("LIVE RECONCILIATION HALT: invalid fee_rate_bps") from exc
            return 0.0

        # Taker fees are applied by the protocol at match time. V2 publishes
        # feeRateBps on the trade; if absent, use the configured crypto default
        # for this 5-minute BTC/ETH/SOL/BNB deployment.
        raw_bps = trade.get("fee_rate_bps") or trade.get("feeRateBps")
        if raw_bps in (None, ""):
            rate = 0.07
        else:
            raw = float(raw_bps)
            rate = raw / 10000.0 if raw > 1.0 else raw
        if rate < 0 or rate > 1:
            raise RuntimeError("LIVE RECONCILIATION HALT: invalid taker fee rate")
        # fee = shares * rate * p * (1-p)
        return None, rate

    def sync_trades(self, trades):
        new_fills = []
        for trade in trades or []:
            tid = str(trade.get("id") or "")
            if not tid or tid in self.seen_trades:
                continue

            status = self._status(trade)
            matched_order = None
            matched_parts = []
            for oid, order in self.orders.items():
                parts = self._maker_fills(trade, oid)
                if parts:
                    matched_order = oid
                    matched_parts = [(shares, price, maker) for shares, price, maker in parts]
                    break
                taker = self._taker_fill(trade, oid)
                if taker:
                    matched_order = oid
                    matched_parts = [(taker[0], taker[1], None)]
                    break

            if not matched_order:
                # Unrelated account trade.
                continue

            if status in FAILED_TRADE_STATUSES:
                self.pending_trades.pop(tid, None)
                self.seen_trades.add(tid)
                self.orders[matched_order]["last_trade_status"] = status
                self.save()
                continue

            if status not in CONFIRMED_TRADE_STATUSES:
                # MATCHED is intentionally not booked. It is persisted so a
                # restart cannot forget that an unresolved trade exists.
                self.pending_trades[tid] = {
                    "order_id": matched_order,
                    "status": status or "UNKNOWN",
                    "last_update": trade.get("last_update") or trade.get("timestamp") or time.time(),
                }
                self.orders[matched_order]["last_trade_status"] = status or "UNKNOWN"
                self.save()
                continue

            order = self.orders[matched_order]
            remaining_reserved = max(
                0.0, float(order.get("reserved", 0.0)) - float(order.get("filled_cost", 0.0))
            )
            total_cost = 0.0
            prepared = []
            for shares, price, maker in matched_parts:
                cost = shares * price
                if cost <= 0:
                    continue
                if cost > remaining_reserved + 1e-7:
                    raise RuntimeError(
                        f"LIVE RECONCILIATION HALT: fill ${cost:.8f} exceeds remaining order reservation ${remaining_reserved:.8f}"
                    )
                fee_info = self._fee_for_confirmed_fill(trade, maker)
                if isinstance(fee_info, tuple):
                    _, rate = fee_info
                    fee = shares * rate * price * (1.0 - price)
                else:
                    fee = float(fee_info)
                prepared.append((shares, price, cost, fee))
                total_cost += cost
                remaining_reserved -= cost + fee

            if not prepared:
                continue

            # Apply the whole confirmed trade atomically in memory, then save.
            for shares, price, cost, fee in prepared:
                key = f"{order['condition']}:{order['token']}"
                pos = self.positions.get(key, {
                    "condition": order["condition"],
                    "token": order["token"],
                    "side": order.get("side", ""),
                    "market": order.get("market", ""),
                    "shares": 0.0,
                    "cost": 0.0,
                    "fees": 0.0,
                    "avg": 0.0,
                })
                pos["shares"] += shares
                pos["cost"] += cost
                pos["fees"] += fee
                pos["avg"] = pos["cost"] / pos["shares"]
                pos["last_trade_id"] = tid
                self.positions[key] = pos
                self.cash -= cost + fee
                order["filled_cost"] = float(order.get("filled_cost", 0.0)) + cost

                fill = {
                    "ts": time.time(),
                    "action": "BUY",
                    "trade_id": tid,
                    "order_id": matched_order,
                    "condition": order["condition"],
                    "token": order["token"],
                    "market": order.get("market", ""),
                    "side": order.get("side", ""),
                    "price": price,
                    "shares": shares,
                    "notional": cost,
                    "fees": fee,
                    "fee_rate_bps": str(trade.get("fee_rate_bps") or trade.get("feeRateBps") or "0"),
                    "status": "FILLED",
                    "transaction_hash": trade.get("transaction_hash") or trade.get("transactionHash", ""),
                    **{k: order[k] for k in (
                        "slug", "asset", "market_id", "start_ts", "end_ts",
                        "regime", "fine_band", "entry_count_before", "burst_position",
                        "seconds_since_previous_trade", "execution_mode", "target_capital",
                        "trajectory_likelihood",
                    ) if k in order},
                }
                self.trades.append(fill)
                new_fills.append(fill)

            self.pending_trades.pop(tid, None)
            self.seen_trades.add(tid)
            order["last_trade_status"] = status
            order["status"] = "FILLED" if remaining_reserved <= 1e-8 else "PARTIAL"
            self.save()
        return new_fills

    def reconcile_orders(self, order_states):
        """Reconcile local active orders against authoritative CLOB order data."""
        active = {"SUBMITTED", "OPEN", "PARTIAL", "LIVE", "MATCHED"}
        for oid, state in (order_states or {}).items():
            rec = self.orders.get(str(oid))
            if not rec:
                raise RuntimeError(f"LIVE RECONCILIATION HALT: unknown order returned by CLOB: {oid}")
            status = str(state.get("status") or "").upper()
            rec["exchange_status"] = status
            rec["exchange_size_matched"] = float(state.get("size_matched") or 0)
            if status in {"CANCELED", "CANCELLED", "FAILED"}:
                rec["status"] = "CLOSED_OR_CANCELED"
            elif status:
                rec["status"] = status if status in active else rec.get("status", status)
        self.save()

    def open_order_reserve(self, open_orders):
        live_ids = set()
        reserve = 0.0
        for order in open_orders or []:
            oid = str(order.get("id") or order.get("orderID") or order.get("orderId") or "")
            if not oid:
                continue
            live_ids.add(oid)
            rec = self.orders.get(oid)
            if rec:
                original = float(rec.get("reserved", 0.0))
                filled = float(rec.get("filled_cost", 0.0))
                reserve += max(0.0, original - filled)
                rec["status"] = str(order.get("status", rec.get("status", "OPEN")))
                if order.get("size_matched") is not None:
                    rec["exchange_size_matched"] = float(order.get("size_matched") or 0)

        for oid, rec in self.orders.items():
            if oid not in live_ids and rec.get("status") in {"SUBMITTED", "OPEN", "PARTIAL", "LIVE"}:
                # It may have just filled/canceled. The next get_order/trade
                # reconciliation is authoritative; don't erase fill state.
                rec["status"] = "CLOSED_OR_CANCELED"
        self.save()
        return reserve

    def total_open_cost(self):
        return sum(float(p.get("cost", 0.0)) for p in self.positions.values())

    def total_reserved(self):
        return sum(
            max(0.0, float(o.get("reserved", 0.0)) - float(o.get("filled_cost", 0.0)))
            for o in self.orders.values()
            if o.get("status") in {"SUBMITTED", "OPEN", "PARTIAL", "LIVE"}
        )

    def reserved_for_condition(self, condition, open_orders):
        live_ids = {str(o.get("id") or o.get("orderID") or o.get("orderId") or "") for o in (open_orders or [])}
        total = 0.0
        for oid in live_ids:
            rec = self.orders.get(oid)
            if rec and rec.get("condition") == condition:
                total += max(0.0, float(rec.get("reserved", 0.0)) - float(rec.get("filled_cost", 0.0)))
        return total

    def exposure(self, condition):
        return sum(float(p.get("cost", 0.0)) for p in self.positions.values() if p.get("condition") == condition)

    def positions_for(self, condition):
        return [p for p in self.positions.values() if p.get("condition") == condition]

    def settle(self, condition, winner_token):
        closed = []
        for key, p in list(self.positions.items()):
            if p.get("condition") != condition:
                continue
            shares = float(p["shares"])
            cost = float(p["cost"])
            fees = float(p.get("fees", 0.0))
            payout = shares if str(p.get("token")) == str(winner_token) else 0.0
            pnl = payout - cost - fees
            closed.append({
                "key": key, "pnl": pnl, "payout": payout, "cost": cost,
                "fees": fees, "shares": shares, "token": p["token"],
                "side": p.get("side", ""),
            })

        if not closed:
            return []

        self.cash += sum(x["payout"] for x in closed)
        self.realized += sum(x["pnl"] for x in closed)
        for x in closed:
            p = self.positions[x["key"]]
            self.trades.append({
                "ts": time.time(), "action": "SETTLE", "condition": condition,
                "token": p["token"], "side": p.get("side", ""),
                "price": p.get("avg", 0.0), "shares": x["shares"],
                "notional": x["cost"], "payout": x["payout"],
                "fees": x["fees"], "pnl": x["pnl"],
                "status": "WIN" if x["pnl"] >= 0 else "LOSS",
            })
            del self.positions[x["key"]]
        self.save()
        return closed

    def equity(self, books):
        value = 0.0
        for p in self.positions.values():
            bid = books.get(p["token"])
            px = bid if bid is not None else p.get("avg", 0.0)
            value += float(p["shares"]) * float(px)
        return self.cash + value
