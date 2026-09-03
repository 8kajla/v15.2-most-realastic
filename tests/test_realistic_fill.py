import json
from pathlib import Path

from realistic_fill_simulator import RealisticFillSimulator


def test_pending_order_does_not_fill_before_trade(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    o = sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=1.0, placed_ts=100.0, window_end_ts=400.0, depth_ahead=5.0,
        meta={"asset": "BTC", "regime": "CHEAP", "fine_band": "C15_20"},
    )
    assert o["status"] == "PENDING"
    assert sim.drain_fills() == []
    assert sim.metrics(101.0)["pending"] == 1


def test_queue_ahead_requires_trade_volume_before_fill(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=1.0, placed_ts=100.0, window_end_ts=400.0, depth_ahead=5.0,
        meta={"regime": "CHEAP", "fine_band": "C15_20"},
    )
    sim.on_trade_print("t1", 0.20, 5.0, 110.0, "trade-a", "SELL")
    assert sim.drain_fills() == []
    sim.on_trade_print("t1", 0.19, 6.0, 111.0, "trade-b", "SELL")
    fills = sim.drain_fills()
    assert len(fills) == 1
    assert fills[0]["shares"] == 5.0
    assert fills[0]["price"] == 0.19
    assert fills[0]["fill_latency_s"] == 11.0
    assert sim.metrics(112.0)["filled"] == 1


def test_buy_trade_does_not_fill_resting_buy(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Down", target_price=0.30,
        notional=0.9, placed_ts=100.0, window_end_ts=400.0, depth_ahead=0.0,
    )
    sim.on_trade_print("t1", 0.25, 100.0, 101.0, "trade-buy", "BUY")
    assert sim.drain_fills() == []


def test_partial_then_full_fill_is_accounted_once_per_increment(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=2.0, placed_ts=100.0, window_end_ts=400.0, depth_ahead=0.0,
        meta={"regime": "MID", "fine_band": "M40_50"},
    )
    sim.on_trade_print("t1", 0.20, 4.0, 110.0, "trade-1", "SELL")
    first = sim.drain_fills()
    assert first[0]["shares"] == 4.0
    assert first[0]["status"] == "PARTIAL"
    sim.on_trade_print("t1", 0.19, 6.0, 111.0, "trade-2", "SELL")
    second = sim.drain_fills()
    assert second[0]["shares"] == 6.0
    assert second[0]["status"] == "FILLED"
    assert sim.metrics(112.0)["filled"] == 1


def test_order_expires_at_market_end_without_ledger_fill(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=1.0, placed_ts=100.0, window_end_ts=200.0, depth_ahead=10.0,
        meta={"regime": "HIGH", "fine_band": "H90_95"},
    )
    expired = sim.expire(200.0)
    assert len(expired) == 1
    assert expired[0]["status"] == "EXPIRED_UNFILLED"
    assert sim.drain_fills() == []
    assert sim.metrics(201.0)["expired_unfilled"] == 1


def test_later_same_price_order_is_behind_earlier_order(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=1.0, placed_ts=100.0, window_end_ts=400.0, depth_ahead=0.0,
    )
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=1.0, placed_ts=101.0, window_end_ts=400.0, depth_ahead=0.0,
    )
    sim.on_trade_print("t1", 0.20, 5.0, 102.0, "trade-1", "SELL")
    fills = sim.drain_fills()
    assert len(fills) == 1
    assert fills[0]["order_id"]
    orders = sim.all_orders()
    filled = [x for x in orders if x["status"] == "FILLED"]
    partial = [x for x in orders if x["status"] == "PARTIAL"]
    assert len(filled) == 1
    assert len(partial) == 0

from paper_ledger import PaperLedger


def test_reconcile_recovers_persisted_fill_not_yet_booked(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=2.0, placed_ts=100.0, window_end_ts=400.0, depth_ahead=0.0,
        meta={"sim_order_id": "placeholder", "regime": "MID", "fine_band": "M40_50"},
    )
    sim.on_trade_print("t1", 0.18, 20.0, 101.0, "trade-1", "SELL")
    sim.drain_fills()
    ledger = PaperLedger(tmp_path / "ledger.json", 100.0)
    recovery = sim.reconcile_ledger(ledger)
    assert len(recovery) == 1
    assert recovery[0]["shares"] == 10.0
    assert abs(recovery[0]["notional"] - 1.8) < 1e-12
    ledger.buy("c1", "t1", "m1", "Up", recovery[0]["price"], recovery[0]["notional"], recovery[0]["fill_ts"], {"sim_order_id": recovery[0]["order_id"]})
    assert sim.reconcile_ledger(ledger) == []


def test_recovery_handles_partial_fill_delta(tmp_path):
    sim = RealisticFillSimulator(tmp_path / "orders.json")
    o = sim.place_order(
        condition="c1", market="m1", token="t1", side="Up", target_price=0.20,
        notional=2.0, placed_ts=100.0, window_end_ts=400.0, depth_ahead=0.0,
    )
    sim.on_trade_print("t1", 0.20, 3.0, 101.0, "trade-1", "SELL")
    first = sim.drain_fills()[0]
    ledger = PaperLedger(tmp_path / "ledger.json", 100.0)
    ledger.buy("c1", "t1", "m1", "Up", first["price"], first["notional"], first["fill_ts"], {"sim_order_id": o["id"]})
    sim.on_trade_print("t1", 0.18, 7.0, 102.0, "trade-2", "SELL")
    sim.drain_fills()
    rec = sim.reconcile_ledger(ledger)
    assert len(rec) == 1
    assert abs(rec[0]["shares"] - 7.0) < 1e-12
    assert abs(rec[0]["notional"] - 1.26) < 1e-12


def test_market_feed_parses_last_trade_event():
    from feeds.market_book import PolymarketMarketFeed
    events = []
    feed = PolymarketMarketFeed()
    feed.set_trade_callback(lambda *args: events.append(args))
    feed._handle({
        "event_type": "last_trade_price", "asset_id": "t1", "price": "0.19",
        "size": "12.5", "side": "SELL", "timestamp": "1700000000123",
        "transaction_hash": "0xhash", "id": "trade-id",
    })
    assert len(events) == 1
    token, price, size, ts, tid, side, tx = events[0]
    assert token == "t1" and price == 0.19 and size == 12.5
    assert ts == 1700000000.123 and tid == "trade-id" and side == "SELL" and tx == "0xhash"
