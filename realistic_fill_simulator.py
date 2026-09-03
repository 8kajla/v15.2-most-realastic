from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Dict, Iterable, List, Optional


PENDING_STATUSES = {"PENDING", "PARTIAL"}
FINAL_STATUSES = {"FILLED", "EXPIRED_UNFILLED", "CANCELED"}


class RealisticFillSimulator:
    """Shadow resting-maker simulator fed by public CLOB trade-print events.

    This is deliberately a research approximation.  It captures queue-ahead
    depth at order placement and advances that queue with subsequent qualifying
    SELL prints.  It cannot know our true queue priority, hidden liquidity, or
    market impact.  It therefore never labels a synthetic fill as equivalent to
    a real exchange fill.
    """

    def __init__(self, path: Path, websocket_feed=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orders: Dict[str, dict] = {}
        self.seen_trade_ids = set()
        # Secondary dedupe indexes protect against feed/reconnect duplicates
        # that arrive with a different event id but the same on-chain trade.
        self.seen_trade_tx_keys = set()
        self.seen_trade_fingerprints = set()
        self._fill_events: List[dict] = []
        self._lock = threading.RLock()
        self.feed = websocket_feed
        self._load()
        self.save()

    def _load(self):
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.orders = data.get("orders", {})
        self.seen_trade_ids = set(data.get("seen_trade_ids", []))
        self.seen_trade_tx_keys = set(data.get("seen_trade_tx_keys", []))
        self.seen_trade_fingerprints = set(data.get("seen_trade_fingerprints", []))

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "orders": self.orders,
                        "seen_trade_ids": list(self.seen_trade_ids)[-100000:],
                        "seen_trade_tx_keys": list(self.seen_trade_tx_keys)[-100000:],
                        "seen_trade_fingerprints": list(self.seen_trade_fingerprints)[-100000:],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    @staticmethod
    def _price_key(price: float) -> str:
        return f"{float(price):.10f}"

    def start(self):
        if self.feed is not None:
            self.feed.set_trade_callback(self.on_trade_print)
            self.feed.start()

    def stop(self):
        if self.feed is not None:
            self.feed.stop()

    def subscribe_token(self, token: str):
        if self.feed is not None:
            self.feed.subscribe(str(token))

    def place_order(
        self,
        *,
        condition: str,
        market: str,
        token: str,
        side: str,
        target_price: float,
        notional: float,
        placed_ts: float,
        window_end_ts: float,
        depth_ahead: float,
        meta: Optional[dict] = None,
    ) -> dict:
        target_price = float(target_price)
        notional = float(notional)
        if not (0.0 < target_price < 1.0):
            raise ValueError("realistic-fill target price must be between 0 and 1")
        if notional <= 0:
            raise ValueError("realistic-fill notional must be positive")
        shares = notional / target_price
        oid = f"paper-maker-{int(placed_ts * 1000)}-{uuid.uuid4().hex[:12]}"
        prior_queue = 0.0
        with self._lock:
            for prior in self.orders.values():
                if (str(prior.get("token")) == str(token)
                        and abs(float(prior.get("target_price", -1.0)) - target_price) <= 1e-9
                        and float(prior.get("placed_ts", placed_ts)) <= float(placed_ts)
                        and prior.get("status") in PENDING_STATUSES):
                    prior_queue += max(0.0, float(prior.get("remaining_shares", 0.0)))
        queue_ahead_estimate = max(0.0, float(depth_ahead)) + prior_queue
        order = {
            "id": oid,
            "condition": str(condition),
            "market": str(market),
            "token": str(token),
            "side": str(side),
            "target_price": target_price,
            "notional": notional,
            "target_shares": shares,
            "remaining_shares": shares,
            "placed_ts": float(placed_ts),
            "window_end_ts": float(window_end_ts),
            "depth_ahead": max(0.0, float(depth_ahead)),
            "queue_ahead_estimate": queue_ahead_estimate,
            "cumulative_volume_through_price": 0.0,
            "status": "PENDING",
            "fill_latency_s": None,
            "filled_shares": 0.0,
            "filled_cost": 0.0,
            "last_fill_price": None,
            "last_fill_ts": None,
            "meta": dict(meta or {}),
        }
        with self._lock:
            self.orders[oid] = order
        self.subscribe_token(token)
        self.save()
        return dict(order)

    @staticmethod
    def _trade_fingerprint(token: str, trade_price: float, trade_size: float,
                           trade_ts: float, trade_side: str) -> str:
        # Millisecond timestamp is deliberately used here: the public feed's
        # timestamp resolution is coarse enough that reconnect/replay duplicates
        # normally retain the same event time while genuine prints remain distinct.
        raw = (f"{str(token)}|{float(trade_price):.10f}|{float(trade_size):.10f}|"
               f"{float(trade_ts):.3f}|{str(trade_side).upper()}").encode()
        return sha256(raw).hexdigest()

    def _trade_already_seen(self, token: str, trade_price: float, trade_size: float,
                            trade_ts: float, trade_id: str, trade_side: str,
                            transaction_hash: str) -> bool:
        event_id = str(trade_id or "").strip()
        tx = str(transaction_hash or "").strip()
        fingerprint = self._trade_fingerprint(token, trade_price, trade_size, trade_ts, trade_side)
        # Prefer the explicit event id, but use tx+payload and payload-only
        # fingerprints as defensive secondary identity when ids are unstable.
        if event_id and event_id in self.seen_trade_ids:
            return True
        if tx and f"{tx}|{fingerprint}" in self.seen_trade_tx_keys:
            return True
        if fingerprint in self.seen_trade_fingerprints:
            return True
        if event_id:
            self.seen_trade_ids.add(event_id)
        if tx:
            self.seen_trade_tx_keys.add(f"{tx}|{fingerprint}")
        self.seen_trade_fingerprints.add(fingerprint)
        return False

    def _qualifies(self, order: dict, trade_price: float, trade_side: str) -> bool:
        if order.get("status") not in PENDING_STATUSES:
            return False
        if str(order.get("side")) != "Up" and str(order.get("side")) != "Down":
            return False
        # This simulator is specifically for a resting BUY.  The public market
        # WebSocket reports the aggressive side on last_trade_price events.
        if str(trade_side).upper() != "SELL":
            return False
        return float(trade_price) <= float(order["target_price"]) + 1e-9

    def on_trade_print(
        self,
        token: str,
        trade_price: float,
        trade_size: float,
        trade_ts: Optional[float] = None,
        trade_id: Optional[str] = None,
        trade_side: str = "",
        transaction_hash: str = "",
    ):
        trade_price = float(trade_price)
        trade_size = max(0.0, float(trade_size))
        trade_ts = float(trade_ts if trade_ts is not None else time.time())
        trade_id = str(trade_id or transaction_hash or f"{token}|{trade_ts:.6f}|{trade_price:.10f}|{trade_size:.10f}")
        if trade_size <= 0:
            return
        with self._lock:
            if self._trade_already_seen(token, trade_price, trade_size, trade_ts,
                                        trade_id, trade_side, transaction_hash):
                return
            changed = False
            # Every qualifying public SELL print is applied once to each resting
            # order that has been alive long enough to see that print. Each order
            # carries its own cumulative tape volume since placement, while
            # queue_ahead_estimate includes the displayed depth plus earlier
            # simulator orders at the same price. This prevents the same trade
            # volume from magically filling multiple orders that were behind one
            # another in the simulated queue.
            for order in sorted(self.orders.values(), key=lambda x: float(x.get("placed_ts", 0.0))):
                if str(order.get("token")) != str(token):
                    continue
                if order.get("status") not in PENDING_STATUSES:
                    continue
                if trade_ts < float(order.get("placed_ts", 0.0)):
                    continue
                if trade_ts >= float(order.get("window_end_ts", float("inf"))):
                    continue
                if not self._qualifies(order, trade_price, trade_side):
                    continue

                order["cumulative_volume_through_price"] = (
                    float(order.get("cumulative_volume_through_price", 0.0)) + trade_size
                )
                queue_ahead = float(order.get("queue_ahead_estimate", order.get("depth_ahead", 0.0)))
                fillable_total = max(0.0, float(order["cumulative_volume_through_price"]) - queue_ahead)
                already_filled = float(order.get("filled_shares", 0.0))
                newly_fillable = max(0.0, fillable_total - already_filled)
                fill_shares = min(float(order.get("remaining_shares", 0.0)), newly_fillable)
                changed = True
                if fill_shares <= 1e-12:
                    continue

                order["remaining_shares"] = max(0.0, float(order["remaining_shares"]) - fill_shares)
                order["filled_shares"] = already_filled + fill_shares
                fill_cost = fill_shares * trade_price
                order["filled_cost"] = float(order.get("filled_cost", 0.0)) + fill_cost
                order["last_fill_price"] = trade_price
                order["last_fill_ts"] = trade_ts
                if order["fill_latency_s"] is None:
                    order["fill_latency_s"] = max(0.0, trade_ts - float(order["placed_ts"]))

                if float(order["remaining_shares"]) <= 1e-12:
                    order["status"] = "FILLED"
                else:
                    order["status"] = "PARTIAL"

                self._fill_events.append(
                    {
                        "order_id": order["id"],
                        "sim_order_id": order["id"],
                        "condition": order["condition"],
                        "market": order["market"],
                        "token": order["token"],
                        "side": order["side"],
                        "shares": fill_shares,
                        "price": trade_price,
                        "notional": fill_cost,
                        "fill_ts": trade_ts,
                        "placed_ts": order["placed_ts"],
                        "fill_latency_s": order["fill_latency_s"],
                        "status": order["status"],
                        "target_price": order["target_price"],
                        "trade_id": trade_id,
                        "transaction_hash": transaction_hash,
                        "depth_ahead": order["depth_ahead"],
                        "queue_ahead_estimate": queue_ahead,
                        "cumulative_volume_through_price": order["cumulative_volume_through_price"],
                        "meta": dict(order.get("meta") or {}),
                    }
                )
            if changed:
                self.save()

    def expire(self, now: Optional[float] = None) -> List[dict]:
        now = float(now if now is not None else time.time())
        expired = []
        with self._lock:
            for order in self.orders.values():
                if order.get("status") in PENDING_STATUSES and now >= float(order["window_end_ts"]):
                    order["status"] = "EXPIRED_UNFILLED"
                    order["expired_ts"] = now
                    order["unfilled_shares"] = max(0.0, float(order.get("remaining_shares", 0.0)))
                    expired.append(dict(order))
            if expired:
                self.save()
        return expired

    def cancel_condition(self, condition: str, now: Optional[float] = None, reason: str = "CANCELED") -> List[dict]:
        now = float(now if now is not None else time.time())
        out = []
        with self._lock:
            for order in self.orders.values():
                if order.get("condition") == str(condition) and order.get("status") in PENDING_STATUSES:
                    order["status"] = "CANCELED"
                    order["canceled_ts"] = now
                    order["cancel_reason"] = reason
                    out.append(dict(order))
            if out:
                self.save()
        return out

    def drain_fills(self) -> List[dict]:
        with self._lock:
            out = list(self._fill_events)
            self._fill_events.clear()
            return out

    def pending(self) -> List[dict]:
        with self._lock:
            return [dict(x) for x in self.orders.values() if x.get("status") in PENDING_STATUSES]

    def all_orders(self) -> List[dict]:
        with self._lock:
            return [dict(x) for x in self.orders.values()]

    def reconcile_ledger(self, ledger) -> List[dict]:
        """Return aggregate filled-value deltas not yet booked in the paper ledger.

        This closes the crash window between the simulator persisting a fill and
        the main loop writing the corresponding PaperLedger BUY.  Aggregating a
        missing delta at its weighted-average fill price preserves shares/cost
        exactly even when the original process had several partial fills.
        """
        booked = defaultdict(lambda: {"shares": 0.0, "cost": 0.0})
        for trade in getattr(ledger, "trades", []):
            if trade.get("action") != "BUY":
                continue
            sim_id = (trade.get("sim_order_id") or trade.get("meta", {}).get("sim_order_id"))
            if not sim_id:
                continue
            booked[str(sim_id)]["shares"] += float(trade.get("shares", 0.0))
            booked[str(sim_id)]["cost"] += float(trade.get("notional", 0.0))

        out = []
        with self._lock:
            for order in self.orders.values():
                target_shares = float(order.get("filled_shares", 0.0))
                target_cost = float(order.get("filled_cost", 0.0))
                cur = booked[str(order.get("id"))]
                delta_shares = target_shares - cur["shares"]
                delta_cost = target_cost - cur["cost"]
                if delta_shares <= 1e-9 or delta_cost <= 1e-12:
                    continue
                price = delta_cost / delta_shares
                meta = dict(order.get("meta") or {})
                meta["sim_order_id"] = order["id"]
                meta["realistic_fill_recovery"] = True
                out.append({
                    "order_id": order["id"], "condition": order["condition"],
                    "market": order["market"], "token": order["token"],
                    "side": order["side"], "shares": delta_shares, "notional": delta_cost,
                    "price": price, "fill_ts": order.get("last_fill_ts") or time.time(),
                    "placed_ts": order["placed_ts"], "fill_latency_s": order.get("fill_latency_s"),
                    "target_price": order["target_price"], "trade_id": f"recovery-{order['id']}",
                    "depth_ahead": order.get("depth_ahead", 0.0),
                    "queue_ahead_estimate": order.get("queue_ahead_estimate", 0.0),
                    "cumulative_volume_through_price": order.get("cumulative_volume_through_price", 0.0),
                    "status": order.get("status"), "meta": meta,
                })
        return out

    def metrics(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else time.time())
        with self._lock:
            orders = list(self.orders.values())
        by_regime = defaultdict(lambda: {"signals": 0, "filled": 0, "any_filled": 0, "expired": 0, "partial": 0, "fill_cost": 0.0, "signal_notional": 0.0, "fill_latency_s": []})
        by_band = defaultdict(lambda: {"signals": 0, "filled": 0, "any_filled": 0, "expired": 0, "partial": 0, "fill_cost": 0.0, "signal_notional": 0.0, "fill_latency_s": []})
        for order in orders:
            regime = str((order.get("meta") or {}).get("regime") or "OTHER")
            bucket = by_regime[regime]
            bucket["signals"] += 1
            bucket["signal_notional"] += float(order.get("notional", 0.0))
            status = order.get("status")
            has_fill = float(order.get("filled_shares", 0.0)) > 1e-12
            bucket["any_filled"] += int(has_fill)
            if status == "FILLED":
                bucket["filled"] += 1
            elif status == "EXPIRED_UNFILLED":
                bucket["expired"] += 1
            elif status == "PARTIAL":
                bucket["partial"] += 1
            bucket["fill_cost"] += float(order.get("filled_cost", 0.0))
            if order.get("fill_latency_s") is not None:
                bucket["fill_latency_s"].append(float(order["fill_latency_s"]))
            band = str((order.get("meta") or {}).get("fine_band") or "OTHER")
            bb = by_band[band]
            bb["signals"] += 1
            bb["signal_notional"] += float(order.get("notional", 0.0))
            bb["any_filled"] += int(has_fill)
            if status == "FILLED": bb["filled"] += 1
            elif status == "EXPIRED_UNFILLED": bb["expired"] += 1
            elif status == "PARTIAL": bb["partial"] += 1
            bb["fill_cost"] += float(order.get("filled_cost", 0.0))
            if order.get("fill_latency_s") is not None:
                bb["fill_latency_s"].append(float(order["fill_latency_s"]))

        def collapse(b):
            lats = sorted(b["fill_latency_s"])
            b = dict(b)
            b["fill_rate"] = b["filled"] / b["signals"] if b["signals"] else 0.0
            b["any_fill_rate"] = b["any_filled"] / b["signals"] if b["signals"] else 0.0
            b["expire_rate"] = b["expired"] / b["signals"] if b["signals"] else 0.0
            b["avg_fill_latency_s"] = sum(lats) / len(lats) if lats else None
            b["p50_fill_latency_s"] = lats[(len(lats) - 1) // 2] if lats else None
            b["p90_fill_latency_s"] = lats[min(len(lats) - 1, int(0.9 * len(lats)))] if lats else None
            del b["fill_latency_s"]
            return b

        return {
            "timestamp": now,
            "orders": len(orders),
            "pending": sum(o.get("status") in PENDING_STATUSES for o in orders),
            "filled": sum(o.get("status") == "FILLED" for o in orders),
            "expired_unfilled": sum(o.get("status") == "EXPIRED_UNFILLED" for o in orders),
            "partial": sum(o.get("status") == "PARTIAL" for o in orders),
            "signal_notional": sum(float(o.get("notional", 0.0)) for o in orders),
            "filled_notional": sum(float(o.get("filled_cost", 0.0)) for o in orders),
            "by_regime": {k: collapse(v) for k, v in by_regime.items()},
            "by_band": {k: collapse(v) for k, v in by_band.items()},
        }
