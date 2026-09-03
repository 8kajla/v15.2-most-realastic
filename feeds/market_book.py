from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional, Set

import websocket


log = logging.getLogger("market_book")
WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketMarketFeed:
    """Public market-channel WebSocket for book and trade-print events.

    The market channel is unauthenticated.  Trade events include price, size,
    side and timestamp, which is exactly what the realistic fill simulator
    needs.  Reconnects resubscribe all known tokens.
    """

    def __init__(self, url: str = WSS_URL, reconnect_seconds: float = 2.0):
        self.url = url
        self.reconnect_seconds = max(0.5, float(reconnect_seconds))
        self.tokens: Set[str] = set()
        self._trade_callback: Optional[Callable[..., None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws = None
        self._lock = threading.RLock()

    def set_trade_callback(self, callback):
        self._trade_callback = callback

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="polymarket-market-feed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def subscribe(self, token: str):
        token = str(token)
        with self._lock:
            new = token not in self.tokens
            self.tokens.add(token)
            ws = self._ws
        if new and ws is not None:
            try:
                ws.send(json.dumps({"assets_ids": [token], "operation": "subscribe"}))
            except Exception:
                pass

    def _subscription(self):
        with self._lock:
            tokens = sorted(self.tokens)
        return json.dumps({"assets_ids": tokens, "type": "market"})

    def _run(self):
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self.url, timeout=10, enable_multithread=True)
                ws.settimeout(10)
                with self._lock:
                    self._ws = ws
                if self.tokens:
                    ws.send(self._subscription())
                last_ping = time.time()
                while not self._stop.is_set():
                    if time.time() - last_ping >= 10:
                        try:
                            ws.send("PING")
                        except Exception:
                            break
                        last_ping = time.time()
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    if str(raw).strip().lower() in {"pong", "ping"}:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    self._handle(data)
            except Exception as exc:
                if not self._stop.is_set():
                    log.warning("MARKET WS ERROR | %s: %s", type(exc).__name__, exc)
            finally:
                with self._lock:
                    self._ws = None
                if not self._stop.is_set():
                    self._stop.wait(self.reconnect_seconds)

    def _handle(self, data):
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle(item)
            return
        if not isinstance(data, dict):
            return
        event = str(data.get("event_type") or "").lower()
        if event != "last_trade_price":
            return
        token = data.get("asset_id")
        price = data.get("price")
        size = data.get("size")
        side = data.get("side")
        if token is None or price is None or size is None:
            return
        try:
            ts_ms = float(data.get("timestamp"))
            ts = ts_ms / 1000.0 if ts_ms > 10_000_000_000 else ts_ms
        except (TypeError, ValueError):
            ts = time.time()
        callback = self._trade_callback
        if callback is None:
            return
        try:
            callback(
                str(token),
                float(price),
                float(size),
                ts,
                str(data.get("id") or data.get("transaction_hash") or ""),
                str(side or ""),
                str(data.get("transaction_hash") or ""),
            )
        except Exception:
            log.exception("MARKET WS TRADE CALLBACK ERROR")
