import csv
import json
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path


REGIMES = ("CHEAP", "MID", "CORE", "HIGH")

SCHEMAS = {
    "trades.csv": [
        "trade_id", "timestamp", "market_id", "condition", "slug", "asset",
        "market", "side", "token", "price", "shares", "notional",
        "seconds_into_market", "seconds_remaining", "entry_count_before",
        "burst_position", "seconds_since_previous_trade",
        "up_bid", "up_ask", "up_depth", "down_bid", "down_ask", "down_depth",
        "spread", "score", "momentum", "signal_reason", "cash_after",
        "market_exposure_after", "fine_band", "regime",
    ],
    "markets.csv": [
        "market_id", "condition", "slug", "asset", "market", "start_ts",
        "end_ts", "winner", "entries", "total_cost", "total_shares",
        "avg_entry", "first_entry", "last_entry", "max_exposure", "up_cost",
        "down_cost", "up_shares", "down_shares", "winning_cost",
        "losing_cost", "payout", "realized_pnl", "roi", "resolved_ts",
    ],
    "resolutions.csv": [
        "timestamp", "market_id", "condition", "slug", "asset", "winner",
        "winner_token", "entries", "cost", "payout", "pnl", "roi", "status",
    ],
    "settlement_details.csv": [
        "timestamp", "market_id", "condition", "slug", "asset", "trade_id",
        "side", "token", "regime", "price", "shares", "cost",
        "settlement_per_share", "payout", "pnl", "roi", "outcome",
    ],
    "regime_1min.csv": [
        "timestamp", "regime", "trades", "notional", "trade_share",
        "settled_trades", "wins", "losses", "win_rate", "settled_cost",
        "settled_pnl", "settled_roi", "avg_settled_pnl", "open_cost",
    ],
    "trade_details.csv": [
        "trade_id", "timestamp", "market_id", "condition", "slug", "asset",
        "market", "side", "token", "regime", "fine_band", "price", "shares",
        "notional", "seconds_into_market", "seconds_remaining",
        "entry_count_before", "burst_position", "seconds_since_previous_trade",
        "spread", "score", "momentum", "cash_after",
        "market_exposure_after", "up_bid", "up_ask", "up_depth",
        "down_bid", "down_ask", "down_depth", "signal_reason",
        "trajectory_likelihood",
    ],
    "pnl_1min.csv": [
        "timestamp", "equity", "total_pnl", "realized_pnl",
        "unrealized_pnl", "cash", "open_cost", "market_value",
        "drawdown", "positions", "marked",
    ],
    "realistic_orders.csv": [
        "timestamp", "order_id", "market_id", "condition", "slug", "asset",
        "side", "token", "target_price", "notional", "target_shares",
        "depth_ahead", "window_end_ts", "entry_count_before", "burst_position",
        "fine_band", "regime", "status",
    ],
    "realistic_fills.csv": [
        "timestamp", "order_id", "trade_id", "market_id", "condition", "slug",
        "asset", "side", "token", "target_price", "fill_price", "shares",
        "notional", "depth_ahead", "queue_volume", "fill_latency_s", "status",
        "fill_source",
    ],
    "realistic_unfilled.csv": [
        "timestamp", "order_id", "market_id", "condition", "slug", "asset",
        "side", "token", "target_price", "notional", "remaining_shares",
        "depth_ahead", "placed_ts", "window_end_ts", "fine_band", "regime",
        "status", "reason",
    ],
    "realistic_settlement_details.csv": [
        "timestamp", "market_id", "condition", "slug", "asset", "side", "token",
        "price", "shares", "cost", "payout", "pnl", "outcome",
    ],
    "realistic_fill_metrics_1min.csv": [
        "timestamp", "signals", "filled", "partial", "pending",
        "expired_unfilled", "signal_notional", "filled_notional",
        "fill_rate", "any_fill_rate", "expire_rate", "instant_pnl", "realistic_pnl",
        "realistic_realized_pnl", "realistic_unrealized_pnl",
        "realistic_cash", "realistic_open_cost", "realistic_positions",
    ],
    "realistic_fill_regime_1min.csv": [
        "timestamp", "regime", "signals", "filled", "any_filled", "partial",
        "expired_unfilled", "fill_rate", "any_fill_rate", "expire_rate",
        "signal_notional", "filled_notional", "avg_fill_latency_s",
        "p50_fill_latency_s", "p90_fill_latency_s",
    ],
    "realistic_fill_band_1min.csv": [
        "timestamp", "fine_band", "signals", "filled", "any_filled", "partial",
        "expired_unfilled", "fill_rate", "any_fill_rate", "expire_rate",
        "signal_notional", "filled_notional", "avg_fill_latency_s",
        "p50_fill_latency_s", "p90_fill_latency_s",
    ],
}


class ResearchLogger:
    """Auditable logger for paper execution and trader-behavior research."""

    def __init__(self, data_dir, ledger=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

        self._trade_cache = defaultdict(list)
        self.market_stats = defaultdict(lambda: {
            "entries": 0, "cost": 0.0, "shares": 0.0,
            "first_entry": None, "last_entry": None, "max_exposure": 0.0,
            "asset": "", "market": "", "up_cost": 0.0, "down_cost": 0.0,
            "up_shares": 0.0, "down_shares": 0.0, "slug": "",
            "market_id": "", "start_ts": 0.0, "end_ts": 0.0,
        })
        self.regime_stats = {
            regime: {
                "trades": 0, "notional": 0.0, "settled_trades": 0,
                "wins": 0, "losses": 0, "settled_cost": 0.0,
                "settled_pnl": 0.0, "open_cost": 0.0,
            }
            for regime in REGIMES
        }
        self.last_resolution_error = {}
        self._ensure_files()

        if ledger is not None:
            self.rebuild_from_ledger(ledger)

    def _ensure_files(self):
        for filename, fields in SCHEMAS.items():
            path = self.root / filename
            if not path.exists() or path.stat().st_size == 0:
                with path.open("w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(fields)
        for filename in ("decisions.jsonl", "orderbooks.jsonl"):
            (self.root / filename).touch(exist_ok=True)

    def _append_csv(self, filename, row):
        with self.lock, (self.root / filename).open(
            "a", newline="", encoding="utf-8"
        ) as fh:
            writer = csv.DictWriter(
                fh, fieldnames=SCHEMAS[filename], extrasaction="ignore"
            )
            writer.writerow(row)
            fh.flush()

    def _append_jsonl(self, filename, obj):
        with self.lock, (self.root / filename).open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(
                json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            )
            fh.flush()

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _regime(price):
        p = ResearchLogger._safe_float(price, -1.0)
        if 0.01 <= p < 0.30:
            return "CHEAP"
        if 0.30 <= p < 0.70:
            return "MID"
        if 0.70 <= p < 0.90:
            return "CORE"
        if 0.90 <= p < 1.00:
            return "HIGH"
        return "OTHER"

    @staticmethod
    def _movement(price, history, now):
        p = ResearchLogger._safe_float(price, 0.0)
        out = {}
        for seconds in (1, 3, 5, 10, 30):
            eligible = []
            for item in history or []:
                try:
                    ts = float(item[0] if not isinstance(item, dict) else item["ts"])
                    hp = float(
                        item[1] if not isinstance(item, dict)
                        else item.get("best_bid", item.get("mid"))
                    )
                    if ts <= now - seconds:
                        eligible.append((ts, hp))
                except (TypeError, ValueError, KeyError, IndexError):
                    continue
            out[f"m{seconds}"] = p - eligible[-1][1] if eligible else 0.0
        return out

    @staticmethod
    def _depth_imbalance(bid_depth, ask_depth):
        b = ResearchLogger._safe_float(bid_depth)
        a = ResearchLogger._safe_float(ask_depth)
        total = b + a
        return (b - a) / total if total else 0.0

    def record_decision(self, **kw):
        market = kw["market"]
        signal = kw.get("signal")
        ts = float(kw["ts"])

        ub, ua = kw.get("up_bid"), kw.get("up_ask")
        db, da = kw.get("down_bid"), kw.get("down_ask")
        up_history = kw.get("up_history") or []
        down_history = kw.get("down_history") or []

        up_mid = None
        down_mid = None
        if ub is not None and ua is not None:
            up_mid = (float(ub) + float(ua)) / 2.0
        if db is not None and da is not None:
            down_mid = (float(db) + float(da)) / 2.0

        self._append_jsonl("decisions.jsonl", {
            "t": round(ts, 3),
            "m": market["id"],
            "c": market["condition"],
            "s": market["slug"],
            "a": market["asset"],
            "e": round(float(kw["elapsed"]), 3),
            "r": round(float(kw["left"]), 3),
            "ub": ub, "ua": ua, "ud": kw.get("up_depth"),
            "db": db, "da": da, "dd": kw.get("down_depth"),
            "us": float(ua) - float(ub) if ua is not None and ub is not None else None,
            "ds": float(da) - float(db) if da is not None and db is not None else None,
            "ui": self._depth_imbalance(kw.get("up_depth"), kw.get("up_ask_depth")),
            "di": self._depth_imbalance(kw.get("down_depth"), kw.get("down_ask_depth")),
            "x": signal.side if signal else "WAIT",
            "p": signal.price if signal else None,
            "score": signal.score if signal else None,
            "n": signal.notional if signal else 0.0,
            "reason": signal.reason if signal else "no_signal",
            "ex": kw.get("exposure", 0.0),
            "cash": kw.get("cash", 0.0),
            "entry_count": kw.get("entry_count", 0),
            "burst_position": kw.get("burst_position", 0),
            "seconds_since_previous_trade": kw.get("seconds_since_previous"),
            "up_movement": self._movement(up_mid or ub, up_history, ts) if (up_mid or ub) else {},
            "down_movement": self._movement(down_mid or db, down_history, ts) if (down_mid or db) else {},
            # Explicit event label: this record is a trade if a signal existed.
            "event": "TRADE_CANDIDATE" if signal else "NON_TRADE",
            "capture_latency_ms": kw.get("capture_latency_ms"),
        })

    def record_orderbook(self, **kw):
        market = kw["market"]
        ts = float(kw["ts"])
        up_bid, up_ask = kw.get("up_bid"), kw.get("up_ask")
        down_bid, down_ask = kw.get("down_bid"), kw.get("down_ask")

        self._append_jsonl("orderbooks.jsonl", {
            "t": round(ts, 3),
            "m": market["id"],
            "c": market["condition"],
            "s": market["slug"],
            "a": market["asset"],
            "e": round(float(kw["elapsed"]), 3),
            "r": round(float(kw["left"]), 3),
            "ub": up_bid,
            "ua": up_ask,
            "ud": kw.get("up_depth"),
            "uad": kw.get("up_ask_depth"),
            "db": down_bid,
            "da": down_ask,
            "dd": kw.get("down_depth"),
            "dad": kw.get("down_ask_depth"),
            "ui": self._depth_imbalance(kw.get("up_depth"), kw.get("up_ask_depth")),
            "di": self._depth_imbalance(kw.get("down_depth"), kw.get("down_ask_depth")),
            "us": float(up_ask) - float(up_bid) if up_bid is not None and up_ask is not None else None,
            "ds": float(down_ask) - float(down_bid) if down_bid is not None and down_ask is not None else None,
        })

    def record_trade(self, **kw):
        trade = kw["trade"]
        market = kw["market"]
        trade_id = str(trade.get("trade_id") or f"paper-{uuid.uuid4().hex}")

        price = self._safe_float(trade.get("price"))
        regime = self._regime(price)
        band = trade.get("fine_band")
        trade["trade_id"] = trade_id
        trade["regime"] = regime

        ub, ua = kw.get("up_bid"), kw.get("up_ask")
        db, da = kw.get("down_bid"), kw.get("down_ask")

        if trade["side"] == "Up" and ua is not None and ub is not None:
            spread = float(ua) - float(ub)
        elif trade["side"] == "Down" and da is not None and db is not None:
            spread = float(da) - float(db)
        else:
            spread = None

        row = {
            "trade_id": trade_id,
            "timestamp": trade["ts"],
            "market_id": trade.get("market_id", market["id"]),
            "condition": trade["condition"],
            "slug": trade.get("slug", market["slug"]),
            "asset": trade.get("asset", market["asset"]),
            "market": trade.get("market", market["market"]),
            "side": trade["side"],
            "token": trade["token"],
            "price": trade["price"],
            "shares": trade["shares"],
            "notional": trade["notional"],
            "seconds_into_market": kw["elapsed"],
            "seconds_remaining": kw["left"],
            "entry_count_before": kw.get("entry_count_before", 0),
            "burst_position": kw.get("burst_position", 0),
            "seconds_since_previous_trade": kw.get("seconds_since_previous"),
            "up_bid": ub, "up_ask": ua, "up_depth": kw.get("up_depth"),
            "down_bid": db, "down_ask": da, "down_depth": kw.get("down_depth"),
            "spread": spread,
            "score": kw.get("score"),
            "momentum": kw.get("momentum"),
            "signal_reason": kw.get("reason"),
            "cash_after": kw.get("cash_after"),
            "market_exposure_after": kw.get("exposure_after"),
            "fine_band": band,
            "regime": regime,
        }
        self._append_csv("trades.csv", row)

        trajectory_likelihood = kw.get("score")
        self._append_csv("trade_details.csv", {
            **row,
            "trajectory_likelihood": trajectory_likelihood,
        })

        self._trade_cache[trade["condition"]].append(dict(trade))

        stats = self.market_stats[trade["condition"]]
        notional = self._safe_float(trade["notional"])
        shares = self._safe_float(trade["shares"])

        stats["entries"] += 1
        stats["cost"] += notional
        stats["shares"] += shares
        stats["first_entry"] = (
            trade["ts"] if stats["first_entry"] is None
            else min(stats["first_entry"], trade["ts"])
        )
        stats["last_entry"] = trade["ts"]
        stats["max_exposure"] = max(
            stats["max_exposure"],
            self._safe_float(kw.get("exposure_after")),
        )
        stats["asset"] = trade.get("asset", market["asset"])
        stats["market"] = trade.get("market", market["market"])
        stats["slug"] = market["slug"]
        stats["market_id"] = market["id"]
        stats["start_ts"] = market["start_ts"]
        stats["end_ts"] = market["end_ts"]

        if trade["side"] == "Up":
            stats["up_cost"] += notional
            stats["up_shares"] += shares
        else:
            stats["down_cost"] += notional
            stats["down_shares"] += shares

        if regime in self.regime_stats:
            bucket = self.regime_stats[regime]
            bucket["trades"] += 1
            bucket["notional"] += notional
            bucket["open_cost"] += notional

    def record_realistic_order(self, *, ts, market, signal, notional, target_price, depth_ahead, meta, order_id):
        self._append_csv("realistic_orders.csv", {
            "timestamp": ts, "order_id": order_id,
            "market_id": market["id"], "condition": market["condition"],
            "slug": market["slug"], "asset": market["asset"],
            "side": signal.side, "token": market["up"] if signal.side == "Up" else market["down"],
            "target_price": target_price, "notional": notional,
            "target_shares": notional / target_price if target_price else 0.0,
            "depth_ahead": depth_ahead, "window_end_ts": market["end_ts"],
            "entry_count_before": meta.get("entry_count_before", 0),
            "burst_position": meta.get("burst_position", 0),
            "fine_band": meta.get("fine_band", ""), "regime": meta.get("regime", ""),
            "status": "PENDING",
        })

    def record_realistic_fill(self, fill, trade, market):
        meta = fill.get("meta") or {}
        self._append_csv("realistic_fills.csv", {
            "timestamp": fill.get("fill_ts"), "order_id": fill.get("order_id", ""),
            "trade_id": fill.get("trade_id", trade.get("trade_id", "")),
            "market_id": market.get("id", meta.get("market_id", "")),
            "condition": fill.get("condition", ""), "slug": market.get("slug", meta.get("slug", "")),
            "asset": market.get("asset", meta.get("asset", "")), "side": fill.get("side", ""),
            "token": fill.get("token", ""), "target_price": fill.get("target_price"),
            "fill_price": fill.get("price"), "shares": fill.get("shares"),
            "notional": fill.get("notional"), "depth_ahead": fill.get("depth_ahead"),
            "queue_volume": fill.get("cumulative_volume_through_price"),
            "fill_latency_s": fill.get("fill_latency_s"), "status": fill.get("status", "FILLED"),
            "fill_source": "CLOB_WS_LAST_TRADE",
        })

    def record_realistic_unfilled(self, order, reason):
        meta = order.get("meta") or {}
        self._append_csv("realistic_unfilled.csv", {
            "timestamp": order.get("expired_ts") or order.get("canceled_ts") or time.time(),
            "order_id": order.get("id", ""),
            "market_id": meta.get("market_id", ""), "condition": order.get("condition", ""),
            "slug": meta.get("slug", ""), "asset": meta.get("asset", ""),
            "side": order.get("side", ""), "token": order.get("token", ""),
            "target_price": order.get("target_price"), "notional": order.get("notional"),
            "remaining_shares": order.get("remaining_shares"), "depth_ahead": order.get("depth_ahead"),
            "placed_ts": order.get("placed_ts"), "window_end_ts": order.get("window_end_ts"),
            "fine_band": meta.get("fine_band", ""), "regime": meta.get("regime", ""),
            "status": order.get("status", "EXPIRED_UNFILLED"), "reason": reason,
        })

    def record_realistic_resolution(self, **kw):
        market = kw["market"]
        winner_token = kw["winner_token"]
        for item in kw.get("closed", []):
            won = item.get("token") == winner_token
            self._append_csv("realistic_settlement_details.csv", {
                "timestamp": kw["ts"], "market_id": market["id"],
                "condition": market["condition"], "slug": market["slug"],
                "asset": market["asset"], "side": item.get("side", ""),
                "token": item.get("token", ""), "price": item.get("price", 0.0),
                "shares": item.get("shares", 0.0), "cost": item.get("cost", 0.0),
                "payout": item.get("payout", 0.0), "pnl": item.get("pnl", 0.0),
                "outcome": "WIN" if won else "LOSS",
            })

    def record_realistic_metrics(self, ts, realistic_metrics, simulator_metrics, instant_metrics=None):
        self._append_csv("realistic_fill_metrics_1min.csv", {
            "timestamp": ts, "signals": simulator_metrics.get("orders", 0),
            "filled": simulator_metrics.get("filled", 0), "partial": simulator_metrics.get("partial", 0),
            "pending": simulator_metrics.get("pending", 0),
            "expired_unfilled": simulator_metrics.get("expired_unfilled", 0),
            "signal_notional": simulator_metrics.get("signal_notional", 0.0),
            "filled_notional": simulator_metrics.get("filled_notional", 0.0),
            "fill_rate": (simulator_metrics.get("filled", 0) / simulator_metrics.get("orders", 0)) if simulator_metrics.get("orders", 0) else 0.0,
            "any_fill_rate": (sum(int(b.get("any_filled", 0)) for b in simulator_metrics.get("by_regime", {}).values()) / simulator_metrics.get("orders", 0)) if simulator_metrics.get("orders", 0) else 0.0,
            "expire_rate": (simulator_metrics.get("expired_unfilled", 0) / simulator_metrics.get("orders", 0)) if simulator_metrics.get("orders", 0) else 0.0,
            "instant_pnl": (instant_metrics or {}).get("pnl"), "realistic_pnl": realistic_metrics.get("pnl"),
            "realistic_realized_pnl": realistic_metrics.get("realized"),
            "realistic_unrealized_pnl": realistic_metrics.get("unrealized"),
            "realistic_cash": realistic_metrics.get("cash"),
            "realistic_open_cost": realistic_metrics.get("open_cost"),
            "realistic_positions": realistic_metrics.get("marked", 0),
        })
        for regime, bucket in simulator_metrics.get("by_regime", {}).items():
            self._append_csv("realistic_fill_regime_1min.csv", {
                "timestamp": ts, "regime": regime, "signals": bucket.get("signals", 0),
                "filled": bucket.get("filled", 0), "any_filled": bucket.get("any_filled", 0),
                "partial": bucket.get("partial", 0), "expired_unfilled": bucket.get("expired", 0),
                "fill_rate": bucket.get("fill_rate", 0.0), "any_fill_rate": bucket.get("any_fill_rate", 0.0),
                "expire_rate": bucket.get("expire_rate", 0.0), "signal_notional": bucket.get("signal_notional", 0.0),
                "filled_notional": bucket.get("fill_cost", 0.0),
                "avg_fill_latency_s": bucket.get("avg_fill_latency_s"),
                "p50_fill_latency_s": bucket.get("p50_fill_latency_s"),
                "p90_fill_latency_s": bucket.get("p90_fill_latency_s"),
            })
        for band, bucket in simulator_metrics.get("by_band", {}).items():
            self._append_csv("realistic_fill_band_1min.csv", {
                "timestamp": ts, "fine_band": band, "signals": bucket.get("signals", 0),
                "filled": bucket.get("filled", 0), "any_filled": bucket.get("any_filled", 0),
                "partial": bucket.get("partial", 0), "expired_unfilled": bucket.get("expired", 0),
                "fill_rate": bucket.get("fill_rate", 0.0), "any_fill_rate": bucket.get("any_fill_rate", 0.0),
                "expire_rate": bucket.get("expire_rate", 0.0), "signal_notional": bucket.get("signal_notional", 0.0),
                "filled_notional": bucket.get("fill_cost", 0.0),
                "avg_fill_latency_s": bucket.get("avg_fill_latency_s"),
                "p50_fill_latency_s": bucket.get("p50_fill_latency_s"),
                "p90_fill_latency_s": bucket.get("p90_fill_latency_s"),
            })

    def record_resolution(self, **kw):
        market = kw["market"]
        closed = kw["closed"]
        condition = market["condition"]

        stats = self.market_stats[condition]
        trades = self._trade_cache[condition]
        cost = self._safe_float(stats["cost"])
        pnl = sum(self._safe_float(x.get("pnl")) for x in closed)
        payout = cost + pnl

        by_regime = {}
        for item in trades:
            regime = item.get("regime") or self._regime(item.get("price"))
            trade_cost = self._safe_float(item.get("notional"))
            shares = self._safe_float(item.get("shares"))
            won = item.get("token") == kw["winner_token"]
            trade_payout = shares if won else 0.0
            trade_pnl = trade_payout - trade_cost
            roi = trade_pnl / trade_cost if trade_cost else 0.0

            bucket = by_regime.setdefault(regime, {
                "trades": 0, "wins": 0, "losses": 0,
                "cost": 0.0, "pnl": 0.0,
            })
            bucket["trades"] += 1
            bucket["wins"] += int(won)
            bucket["losses"] += int(not won)
            bucket["cost"] += trade_cost
            bucket["pnl"] += trade_pnl

            self._append_csv("settlement_details.csv", {
                "timestamp": kw["ts"],
                "market_id": market["id"],
                "condition": condition,
                "slug": market["slug"],
                "asset": market["asset"],
                "trade_id": item.get("trade_id", ""),
                "side": item.get("side", ""),
                "token": item.get("token", ""),
                "regime": regime,
                "price": item.get("price", 0.0),
                "shares": shares,
                "cost": trade_cost,
                "settlement_per_share": 1.0 if won else 0.0,
                "payout": trade_payout,
                "pnl": trade_pnl,
                "roi": roi,
                "outcome": "WIN" if won else "LOSS",
            })

            if regime in self.regime_stats:
                bucket = self.regime_stats[regime]
                bucket["settled_trades"] += 1
                bucket["settled_cost"] += trade_cost
                bucket["settled_pnl"] += trade_pnl
                bucket["open_cost"] = max(
                    0.0, bucket["open_cost"] - trade_cost
                )
                if won:
                    bucket["wins"] += 1
                else:
                    bucket["losses"] += 1

        self._append_csv("resolutions.csv", {
            "timestamp": kw["ts"],
            "market_id": market["id"],
            "condition": condition,
            "slug": market["slug"],
            "asset": market["asset"],
            "winner": kw["winner"],
            "winner_token": kw["winner_token"],
            "entries": stats["entries"],
            "cost": cost,
            "payout": payout,
            "pnl": pnl,
            "roi": pnl / cost if cost else 0.0,
            "status": "RESOLVED",
        })

        winning_cost = sum(
            self._safe_float(t.get("notional"))
            for t in trades
            if t.get("token") == kw["winner_token"]
        )

        self._append_csv("markets.csv", {
            "market_id": market["id"],
            "condition": condition,
            "slug": market["slug"],
            "asset": market["asset"],
            "market": market["market"],
            "start_ts": market["start_ts"],
            "end_ts": market["end_ts"],
            "winner": kw["winner"],
            "entries": stats["entries"],
            "total_cost": cost,
            "total_shares": stats["shares"],
            "avg_entry": cost / stats["shares"] if stats["shares"] else 0.0,
            "first_entry": stats["first_entry"],
            "last_entry": stats["last_entry"],
            "max_exposure": stats["max_exposure"],
            "up_cost": stats["up_cost"],
            "down_cost": stats["down_cost"],
            "up_shares": stats["up_shares"],
            "down_shares": stats["down_shares"],
            "winning_cost": winning_cost,
            "losing_cost": max(0.0, cost - winning_cost),
            "payout": payout,
            "realized_pnl": pnl,
            "roi": pnl / cost if cost else 0.0,
            "resolved_ts": kw["ts"],
        })

        self._trade_cache.pop(condition, None)
        self.market_stats.pop(condition, None)

    def record_resolution_error(self, **kw):
        market = kw["market"]
        self.last_resolution_error[market["condition"]] = {
            "timestamp": kw["ts"],
            "status": kw["status"],
        }

    def record_pnl(self, timestamp, metrics):
        self._append_csv("pnl_1min.csv", {
            "timestamp": timestamp,
            "equity": metrics.get("equity"),
            "total_pnl": metrics.get("pnl"),
            "realized_pnl": metrics.get("realized"),
            "unrealized_pnl": metrics.get("unrealized"),
            "cash": metrics.get("cash"),
            "open_cost": metrics.get("open_cost"),
            "market_value": metrics.get("market_value"),
            "drawdown": metrics.get("drawdown"),
            "positions": metrics.get("positions"),
            "marked": metrics.get("marked"),
        })

        total_trades = sum(v["trades"] for v in self.regime_stats.values())
        for regime, stats in self.regime_stats.items():
            settled = stats["settled_trades"]
            self._append_csv("regime_1min.csv", {
                "timestamp": timestamp,
                "regime": regime,
                "trades": stats["trades"],
                "notional": stats["notional"],
                "trade_share": (
                    stats["trades"] / total_trades if total_trades else 0.0
                ),
                "settled_trades": settled,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": stats["wins"] / settled if settled else 0.0,
                "settled_cost": stats["settled_cost"],
                "settled_pnl": stats["settled_pnl"],
                "settled_roi": (
                    stats["settled_pnl"] / stats["settled_cost"]
                    if stats["settled_cost"] else 0.0
                ),
                "avg_settled_pnl": (
                    stats["settled_pnl"] / settled if settled else 0.0
                ),
                "open_cost": stats["open_cost"],
            })

    def rebuild_from_ledger(self, ledger):
        """Rebuild current in-memory research state after a restart."""
        self._trade_cache.clear()
        self.market_stats.clear()
        self.regime_stats = {
            regime: {
                "trades": 0, "notional": 0.0, "settled_trades": 0,
                "wins": 0, "losses": 0, "settled_cost": 0.0,
                "settled_pnl": 0.0, "open_cost": 0.0,
            }
            for regime in REGIMES
        }

        settled_conditions = {
            item.get("condition")
            for item in ledger.trades
            if item.get("action") == "SETTLE"
        }
        open_conditions = {
            position.get("condition")
            for position in ledger.positions.values()
        }
        active_conditions = open_conditions | {
            condition
            for condition in self._trade_conditions_for_unsettled(
                ledger.trades, settled_conditions
            )
        }

        for item in ledger.trades:
            if item.get("action") != "BUY":
                continue
            condition = item.get("condition")
            if condition not in active_conditions:
                continue

            trade = dict(item)
            self._trade_cache[condition].append(trade)

            regime = trade.get("regime") or self._regime(trade.get("price"))
            notional = self._safe_float(trade.get("notional"))
            shares = self._safe_float(trade.get("shares"))

            stats = self.market_stats[condition]
            stats["entries"] += 1
            stats["cost"] += notional
            stats["shares"] += shares
            stats["first_entry"] = (
                trade.get("ts")
                if stats["first_entry"] is None
                else min(stats["first_entry"], trade.get("ts"))
            )
            stats["last_entry"] = trade.get("ts")
            stats["max_exposure"] = max(
                stats["max_exposure"],
                self._safe_float(trade.get("market_exposure_after")),
            )
            stats["asset"] = trade.get("asset", "")
            stats["market"] = trade.get("market", "")
            stats["slug"] = trade.get("slug", "")
            stats["market_id"] = trade.get("market_id", "")
            stats["start_ts"] = self._safe_float(trade.get("start_ts"))
            stats["end_ts"] = self._safe_float(trade.get("end_ts"))

            if trade.get("side") == "Up":
                stats["up_cost"] += notional
                stats["up_shares"] += shares
            else:
                stats["down_cost"] += notional
                stats["down_shares"] += shares

            if regime in self.regime_stats:
                bucket = self.regime_stats[regime]
                bucket["trades"] += 1
                bucket["notional"] += notional
                bucket["open_cost"] += notional

    @staticmethod
    def _trade_conditions_for_unsettled(trades, settled_conditions):
        seen = []
        for item in trades:
            if item.get("action") != "BUY":
                continue
            condition = item.get("condition")
            if condition and condition not in settled_conditions and condition not in seen:
                seen.append(condition)
        return seen

    def maintenance(self):
        decision_days = 7
        orderbook_days = 2
        self._prune_jsonl("decisions.jsonl", decision_days)
        self._prune_jsonl("orderbooks.jsonl", orderbook_days)

    def _prune_jsonl(self, filename, retention_days):
        path = self.root / filename
        if not path.exists():
            return

        cutoff = time.time() - retention_days * 86400.0
        tmp = path.with_suffix(path.suffix + ".tmp")

        with self.lock, path.open("r", encoding="utf-8") as src, tmp.open(
            "w", encoding="utf-8"
        ) as dst:
            for line in src:
                try:
                    obj = json.loads(line)
                    if float(obj.get("t", 0)) >= cutoff:
                        dst.write(line)
                except Exception:
                    # Preserve malformed historical records for audit rather
                    # than silently destroying them.
                    dst.write(line)

        tmp.replace(path)
