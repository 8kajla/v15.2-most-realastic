from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class LiveExecutionQueue:
    """Durable allocator for V15.2 signals that are too small to submit yet.

    This is deliberately outside the strategy. A signal's strategy price, side,
    market and scaled dollar allocation are preserved. The queue only bridges
    the gap between V15.2's $300 virtual sizing and Polymarket's per-market
    minimum executable order size.
    """

    TERMINAL = {"expired", "submitted", "dropped"}

    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("queue state must be a list")
            self.items = data
        except Exception as exc:
            raise RuntimeError(f"LIVE EXECUTION QUEUE CORRUPT: {exc}") from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def enqueue(self, *, condition: str, token: str, side: str, market: str,
                price: float, notional: float, meta: Optional[Dict[str, Any]] = None) -> str:
        amount = float(notional)
        if amount <= 0:
            raise ValueError("cannot queue non-positive notional")
        item_id = f"q-{int(time.time() * 1000)}-{len(self.items)}"
        self.items.append({
            "id": item_id,
            "condition": str(condition),
            "token": str(token),
            "side": str(side),
            "market": str(market),
            "price": float(price),
            "notional": amount,
            "created_at": time.time(),
            "status": "queued",
            "meta": dict(meta or {}),
        })
        self._save()
        return item_id

    def has_pending_signal(
        self, *, condition: str, token: str, side: str, price: float, signal_key: str
    ) -> bool:
        """Return True when the same live signal is already waiting in the queue.

        The strategy can be evaluated repeatedly while an order is waiting for
        Polymarket's market minimum. Without this guard, one unchanged signal is
        appended every loop and its notional is duplicated. Once the queued item
        is submitted or expired it is no longer pending, so a later identical
        strategy decision can legitimately create a new signal.
        """
        key = str(signal_key)
        if not key:
            return False
        for item in self.pending():
            meta = item.get("meta") or {}
            if (
                str(item.get("condition")) == str(condition)
                and str(item.get("token")) == str(token)
                and str(item.get("side")) == str(side)
                and abs(float(item.get("price", 0.0)) - float(price)) <= 1e-12
                and str(meta.get("signal_key", "")) == key
            ):
                return True
        return False

    def pending(self) -> List[Dict[str, Any]]:
        return [x for x in self.items if x.get("status") == "queued" and float(x.get("notional", 0)) > 1e-12]

    def pending_groups(self, condition: str) -> List[Dict[str, Any]]:
        """Aggregate queued allocations that can be represented by one order.

        We only combine allocations when condition, token, side, and exact
        strategy price match. This preserves every signal's price/side/market
        mechanics while allowing several sub-minimum allocations to become one
        exchange-valid order.
        """
        groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        for item in self.pending():
            if str(item.get("condition")) != str(condition):
                continue
            price = float(item.get("price", 0.0))
            key = (
                str(item.get("condition")),
                str(item.get("token")),
                str(item.get("side")),
                f"{price:.10f}",
            )
            g = groups.get(key)
            if g is None:
                g = {
                    "condition": str(item.get("condition")),
                    "token": str(item.get("token")),
                    "side": str(item.get("side")),
                    "price": price,
                    "notional": 0.0,
                    "items": [],
                }
                groups[key] = g
            g["notional"] += max(0.0, float(item.get("notional", 0.0)))
            g["items"].append(item)
        return list(groups.values())

    def executable_groups(
        self,
        condition: str,
        execution_price: Optional[float] = None,
        token: Optional[str] = None,
        side: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return executable queue groups without repricing or cross-price pooling.

        Multiple signals may be combined only when condition, token, side and
        exact strategy price are identical. The caller decides whether the exact
        price is currently passive and whether exchange/capital constraints allow
        submission.
        """
        wanted_token = None if token is None else str(token)
        wanted_side = None if side is None else str(side)
        groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        for item in self.pending():
            if str(item.get("condition")) != str(condition):
                continue
            item_token = str(item.get("token"))
            item_side = str(item.get("side"))
            if wanted_token is not None and item_token != wanted_token:
                continue
            if wanted_side is not None and item_side != wanted_side:
                continue
            price = float(item.get("price", 0.0))
            if not (0.0 < price < 1.0):
                continue
            if execution_price is not None and abs(price - float(execution_price)) > 1e-12:
                continue
            key = (str(item.get("condition")), item_token, item_side, f"{price:.10f}")
            group = groups.get(key)
            if group is None:
                group = {
                    "condition": str(item.get("condition")),
                    "token": item_token,
                    "side": item_side,
                    "price": price,
                    "notional": 0.0,
                    "items": [],
                }
                groups[key] = group
            group["notional"] += max(0.0, float(item.get("notional", 0.0)))
            group["items"].append(item)
        return list(groups.values())

    @staticmethod
    def select_complete_items(
        items: List[Dict[str, Any]], min_cost: float, max_order: float
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Select whole queued allocations whose sum satisfies the exchange minimum.

        No individual strategy allocation is resized. Items that would push the
        aggregate above the single-order cap are skipped so the caller can wait
        for a later compatible allocation instead of creating a synthetic partial
        strategy trade. The returned amount is always the exact sum of selected
        queued items.
        """
        minimum = float(min_cost)
        maximum = float(max_order)
        if minimum <= 0 or maximum <= 0 or minimum > maximum + 1e-12:
            return [], 0.0
        selected: List[Dict[str, Any]] = []
        total = 0.0
        for item in items:
            amount = max(0.0, float(item.get("notional", 0.0)))
            if amount <= 0:
                continue
            if total + amount > maximum + 1e-12:
                continue
            selected.append(item)
            total += amount
            if total + 1e-12 >= minimum:
                return selected, total
        return [], 0.0

    def mark_submitted_group(self, group: Dict[str, Any], submitted_notional: float, order_id: str) -> None:
        """Consume a submitted amount from a same-price queue group FIFO."""
        amount = float(submitted_notional)
        if amount <= 0:
            raise ValueError("submitted group amount must be positive")
        remaining = amount
        for item in group.get("items", []):
            if remaining <= 1e-12:
                break
            available = max(0.0, float(item.get("notional", 0.0)))
            take = min(available, remaining)
            if take > 0:
                self.mark_submitted(item, take, order_id)
                remaining -= take
        if remaining > 1e-8:
            raise RuntimeError(
                f"execution queue accounting error: consumed ${amount:.8f} but "
                f"group contained only ${amount - remaining:.8f}"
            )

    def expire_condition(self, condition: str, reason: str) -> float:
        released = 0.0
        changed = False
        for item in self.items:
            if item.get("status") == "queued" and str(item.get("condition")) == str(condition):
                released += max(0.0, float(item.get("notional", 0)))
                item["status"] = "expired"
                item["reason"] = str(reason)
                changed = True
        if changed:
            self._save()
        return released

    def mark_submitted(self, item: Dict[str, Any], submitted_notional: float, order_id: str) -> None:
        amount = float(submitted_notional)
        remaining = max(0.0, float(item.get("notional", 0)) - amount)
        if remaining <= 1e-9:
            item["notional"] = 0.0
            item["status"] = "submitted"
        else:
            item["notional"] = remaining
            item["last_order_id"] = str(order_id)
            item["status"] = "queued"
        item["last_submitted_notional"] = amount
        item["last_submitted_at"] = time.time()
        item["last_order_id"] = str(order_id)
        self._save()

    def compact(self) -> None:
        # Keep recent history for diagnostics, but never delete queued items.
        terminal = [x for x in self.items if x.get("status") in self.TERMINAL]
        queued = [x for x in self.items if x.get("status") == "queued"]
        self.items = terminal[-5000:] + queued
        self._save()

    @staticmethod
    def min_order_cost(price: float, min_shares: float) -> float:
        p = float(price)
        m = float(min_shares)
        if not (0.0 < p < 1.0):
            raise ValueError("invalid order price")
        if m <= 0:
            raise ValueError("invalid market minimum share size")
        return p * m
