from __future__ import annotations

import json
import logging
import threading
import time
from queue import Empty, Full, Queue
from typing import Callable, Optional, Set, Tuple

import websocket


log = logging.getLogger("market_book")
WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketMarketFeed:
    """Public market-channel WebSocket for book and trade-print events.

    Socket callbacks only normalize/queue events.  Fill processing happens on a
    dedicated worker so disk I/O or simulator locks cannot block the WebSocket
    reader and trigger reconnects/duplicate replays.
    """

    def __init__(self, url: str = WSS_URL, reconnect_seconds: float = 2.0,
                 queue_max: int = 50000):
        self.url = url
        self.reconnect_seconds = max(0.5, float(reconnect_seconds))
        self.tokens: Set[str] = set()
        self._trade_callback: Optional[Callable[..., None]] = None
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws = None
        self._lock = threading.RLock()
        self._trade_queue: Queue[Tuple[str, float, float, float, str, str, str]] = Queue(
            maxsize=max(1000, int(queue_max))
        )

    def set_trade_callback(self, callback):
        self._trade_callback = callback

    def start(self):
        self._stop.clear()
        if not self._worker or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._dispatch_loop, name="polymarket-trade-dispatch", daemon=True
            )
            self._worker.start()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="polymarket-market-feed", daemon=True
        )
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
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3)
        # Do not replay stale events on a later start().
        try:
            while True:
                self._trade_queue.get_nowait()
        except Empty:
            pass

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
                ws = websocket.create_connection(
                    self.url, timeout=10, enable_multithread=True
                )
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

    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                event = self._trade_queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                callback = self._trade_callback
                if callback is not None:
                    callback(*event)
            except Exception:
                log.exception("MARKET WS TRADE CALLBACK ERROR")
            finally:
                self._trade_queue.task_done()

    def _enqueue_trade(self, event):
        try:
            self._trade_queue.put_nowait(event)
        except Full:
            # Never block the WebSocket reader. Dropping a trade-print event is
            # preferable to making the socket slow-consumer and triggering a
            # reconnect storm; the research run records the feed-health warning.
            log.error("MARKET WS TRADE QUEUE FULL | size=%d", self._trade_queue.qsize())

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
        event_tuple = (
            str(token), float(price), float(size), ts,
            str(data.get("id") or data.get("trade_id") or ""),
            str(side or ""),
            str(data.get("transaction_hash") or data.get("transactionHash") or ""),
        )
        callback = self._trade_callback
        if callback is None:
            return
        # Before start() tests/direct use historically called _handle directly;
        # keep that behavior without requiring a live worker.
        if not self._worker or not self._worker.is_alive():
            try:
                callback(*event_tuple)
            except Exception:
                log.exception("MARKET WS TRADE CALLBACK ERROR")
            return
        self._enqueue_trade(event_tuple)
