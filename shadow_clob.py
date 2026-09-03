from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import requests

CLOB = "https://clob.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"


class ShadowCLOB:
    """Unauthenticated Polymarket CLOB execution shadow.

    This NEVER submits an order. It validates the same public market constraints
    and maintains synthetic GTC/post-only orders. A resting BUY is considered
    filled when a subsequent public last-trade observation reports a SELL at or
    below the order price. Because the public REST endpoint exposes last trade
    price/side but not historical queue position, this is an execution shadow,
    not an exact guarantee of real queue priority.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orders: Dict[str, dict] = {}
        self.seen_markers = set()
        self._load()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "v15.2-clob-shadow/1.0"})

    def _load(self):
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.orders = data.get("orders", {})
        self.seen_markers = set(data.get("seen_markers", []))
        # FAK orders cancel any unmatched remainder immediately. Older V15.2
        # shadow state incorrectly persisted partial adaptive orders as LIVE,
        # which made the live ledger reserve phantom cash until market cutoff.
        migrated = False
        for order in self.orders.values():
            if order.get("adaptive") and order.get("status") in {"LIVE", "PARTIAL"}:
                if order.get("fill_reported"):
                    order["status"] = "CANCELED"
                    order["cancel_reason"] = "FAK_REMAINDER"
                    migrated = True
                else:
                    # Keep it available for the one outstanding confirmed fill
                    # report, but never expose the unmatched remainder as open.
                    order["status"] = "CANCELED"
                    order["cancel_reason"] = "FAK_REMAINDER"
                    migrated = True
        if migrated:
            self._save()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "orders": self.orders,
            "seen_markers": list(self.seen_markers)[-50000:],
        }, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def geoblock(self) -> dict:
        try:
            r = self.session.get(GEOBLOCK, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            return {"blocked": None, "error": f"{type(exc).__name__}: {exc}"}


    def preflight(self):
        geo = self.geoblock()
        # Shadow mode never places orders, so a blocked result is informational
        # rather than a trading failure. Public market data remains the only
        # dependency needed for this mode.
        return {
            "signer": "SHADOW-NONE",
            "funder": "SHADOW-NONE",
            "signature_type": 3,
            "balance": float("nan"),
            "allowance": float("nan"),
            "blocked": geo.get("blocked"),
            "country": geo.get("country", "UNKNOWN"),
            "region": geo.get("region", ""),
        }

    def book_details(self, token_id: str) -> dict:
        r = self.session.get(f"{CLOB}/book", params={"token_id": str(token_id)}, timeout=5)
        r.raise_for_status()
        d = r.json()
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        best_bid = max((float(x["price"]) for x in bids if isinstance(x, dict)), default=None)
        best_ask = min((float(x["price"]) for x in asks if isinstance(x, dict)), default=None)
        return {
            "asset_id": str(d.get("asset_id") or token_id),
            "market": str(d.get("market") or ""),
            "min_order_size": float(d.get("min_order_size") or 0.0),
            "tick_size": float(d.get("tick_size") or 0.01),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "last_trade_price": float(d.get("last_trade_price")) if d.get("last_trade_price") not in (None, "") else None,
            "bids": bids,
            "asks": asks,
        }

    def tick_size(self, token_id: str):
        return float(self.book_details(token_id).get("tick_size") or 0.01)

    def minimum_order(self, token_id: str, condition: str, price: float):
        del condition
        d = self.book_details(token_id)
        p = float(price)
        tick = float(d.get("tick_size") or 0.01)
        # Price must sit on the market tick grid. Do not silently round it.
        units = round(p / tick)
        if abs(p - units * tick) > 1e-8:
            raise ValueError(f"price {p:.10f} is not on tick size {tick:.10f}")
        min_shares = float(d.get("min_order_size") or 0.0)
        return p * min_shares, min_shares

    def _next_marker(self, token_id: str, price: float, side: str):
        try:
            r = self.session.get(
                f"{CLOB}/last-trade-price",
                params={"token_id": str(token_id)},
                timeout=5,
            )
            r.raise_for_status()
            d = r.json()
            lp = float(d.get("price")) if d.get("price") not in (None, "") else None
            ls = str(d.get("side") or "").upper()
        except Exception:
            return None
        return {"price": lp, "side": ls}

    def adaptive_buy(self, token_id: str, limit_price: float, shares: float, condition: str):
        """Simulate a marketable FAK BUY using the current public best ask.

        Only displayed top-of-book ask liquidity is credited. This deliberately
        understates fills versus the full depth so the shadow result is not falsely
        optimistic.
        """
        limit = float(limit_price)
        requested = float(shares)
        d = self.book_details(token_id)
        ask = d.get("best_ask")
        ask_size = sum(float(x.get("size", 0.0)) for x in (d.get("asks") or [])
                       if isinstance(x, dict) and abs(float(x.get("price", 0.0)) - float(ask or -1)) < 1e-9)
        min_shares = float(d.get("min_order_size") or 0.0)
        if ask is None or float(ask) > limit + 1e-9:
            raise ValueError("shadow adaptive order has no acceptable ask")
        if requested + 1e-9 < min_shares:
            raise ValueError("shadow adaptive order below market minimum")
        fill_shares = min(requested, max(0.0, ask_size))
        oid = f"shadow-fak-{int(time.time()*1000)}-{uuid.uuid4().hex[:10]}"
        self.orders[oid] = {
            "id": oid, "condition": str(condition), "token": str(token_id),
            "side": "BUY", "price": float(ask), "limit_price": limit,
            "notional": float(fill_shares * float(ask)),
            "shares": requested, "remaining_shares": max(0.0, requested - fill_shares),
            # FAK: any unmatched remainder is canceled, never left resting.
            "status": "FILLED" if fill_shares + 1e-9 >= requested else "CANCELED",
            "created_at": time.time(), "last_seen": time.time(),
            "min_order_cost": float(ask) * min_shares, "min_shares": min_shares,
            "adaptive": True,
            "fill_reported": False,
        }
        self._save()
        return {
            "success": True, "orderID": oid,
            "status": "matched" if fill_shares > 0 else "live",
            "makingAmount": float(fill_shares * float(ask)),
            "takingAmount": float(fill_shares), "shadow": True, "adaptive": True,
        }

    def post_only_buy(self, token_id: str, price: float, notional: float, condition: str):
        p = float(price)
        n = float(notional)
        if not (0.0 < p < 1.0):
            raise ValueError("invalid price")
        minimum, min_shares = self.minimum_order(token_id, condition, p)
        if n + 1e-9 < minimum:
            raise ValueError(f"shadow order below market minimum ${minimum:.6f}")
        d = self.book_details(token_id)
        ask = d.get("best_ask")
        if ask is None or float(ask) <= p + 1e-9:
            raise ValueError("shadow post-only order would be marketable")
        oid = f"shadow-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
        shares = n / p
        self.orders[oid] = {
            "id": oid,
            "condition": str(condition),
            "token": str(token_id),
            "side": "BUY",
            "price": p,
            "notional": n,
            "shares": shares,
            "remaining_shares": shares,
            "status": "LIVE",
            "created_at": time.time(),
            "last_seen": None,
            "min_order_cost": minimum,
            "min_shares": min_shares,
        }
        self._save()
        return {"success": True, "orderID": oid, "status": "live", "makingAmount": n, "takingAmount": shares, "shadow": True}

    def get_open_orders(self) -> List[dict]:
        out = []
        for o in self.orders.values():
            if o.get("status") in {"LIVE", "PARTIAL"} and float(o.get("remaining_shares", 0)) > 1e-9:
                out.append({
                    "id": o["id"],
                    "status": "LIVE" if o.get("status") == "LIVE" else "PARTIAL",
                    "asset_id": o["token"],
                    "market": o["condition"],
                    "side": "BUY",
                    "original_size": o["shares"],
                    "size_matched": o["shares"] - o["remaining_shares"],
                    "price": o["price"],
                    "order_type": "GTC",
                })
        return out

    def reconcile_orders(self, order_ids):
        states = {}
        for oid in order_ids:
            o = self.orders.get(str(oid))
            if o:
                states[str(oid)] = {
                    "status": "ORDER_STATUS_LIVE" if o.get("status") in {"LIVE", "PARTIAL"} else str(o.get("status")),
                    "size_matched": o["shares"] - o["remaining_shares"],
                }
        return states

    def get_trades(self):
        fills = []
        now = time.time()
        for oid, order in list(self.orders.items()):
            if order.get("adaptive") and not order.get("fill_reported"):
                fill_shares = max(0.0, float(order.get("shares", 0.0)) - float(order.get("remaining_shares", 0.0)))
                if fill_shares > 1e-9:
                    order["fill_reported"] = True
                    fills.append({
                        "id": f"shadow-trade-{uuid.uuid4().hex}",
                        "status": "CONFIRMED", "trader_side": "TAKER",
                        "taker_order_id": oid,
                        "maker_orders": [],
                        "price": order["price"],
                        "fee_rate_bps": "7",
                        "transaction_hash": "", "shadow": True, "adaptive": True,
                        "order_status": order.get("status", "CANCELED"),
                        "filled_shares": fill_shares,
                        "filled_cost": fill_shares * float(order["price"]),
                        "remaining_shares": float(order.get("remaining_shares", 0.0)),
                    })
                continue
            if order.get("status") not in {"LIVE", "PARTIAL"}:
                continue
            marker = self._next_marker(order["token"], order["price"], "SELL")
            if not marker or marker.get("side") != "SELL" or marker.get("price") is None:
                continue
            trade_price = float(marker["price"])
            if trade_price > float(order["price"]) + 1e-9:
                continue
            # REST last-trade lacks a trade ID/size. A unique (price, side) marker
            # prevents duplicate booking while we stay conservative about queue
            # certainty. We book the whole remaining synthetic order on a
            # qualifying SELL print; this is explicitly labeled SHADOW, not LIVE.
            marker_key = f"{oid}|{trade_price:.10f}|SELL"
            if marker_key in self.seen_markers:
                continue
            self.seen_markers.add(marker_key)
            shares = float(order["remaining_shares"])
            order["remaining_shares"] = 0.0
            order["status"] = "FILLED"
            order["last_seen"] = now
            tid = f"shadow-trade-{uuid.uuid4().hex}"
            fills.append({
                "id": tid,
                "status": "CONFIRMED",
                "trader_side": "MAKER",
                "maker_orders": [{
                    "order_id": oid,
                    "matched_amount": shares,
                    "price": order["price"],
                }],
                "price": order["price"],
                "fee_rate_bps": "0",
                "transaction_hash": "",
                "shadow": True,
            })
        if fills:
            self._save()
        return fills

    def cancel_market_orders(self, condition: str):
        changed = False
        for o in self.orders.values():
            if o.get("condition") == str(condition) and o.get("status") in {"LIVE", "PARTIAL"}:
                o["status"] = "CANCELED"
                changed = True
        if changed:
            self._save()
        return {"success": True, "shadow": True}

    def cancel_all(self):
        changed = False
        for o in self.orders.values():
            if o.get("status") in {"LIVE", "PARTIAL"}:
                o["status"] = "CANCELED"
                changed = True
        if changed:
            self._save()
        return {"success": True, "shadow": True}

    def heartbeat(self):
        return {"success": True, "shadow": True}

    def total_reserved(self):
        return sum(
            max(0.0, float(o.get("notional", 0.0)) * float(o.get("remaining_shares", 0.0)) / max(float(o.get("shares", 1.0)), 1e-12))
            for o in self.orders.values()
            if o.get("status") in {"LIVE", "PARTIAL"}
        )
