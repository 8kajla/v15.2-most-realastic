import logging
import os
import shutil
import time
import traceback
from pathlib import Path

from market_discovery import discover, book, resolve, book_depth_at_price
from paper_ledger import PaperLedger
from live_ledger import LiveLedger
from live_risk import LiveRisk
from live_execution import LiveExecutionQueue
from clob_adaptive import CLOBAdaptivePlanner
from shadow_clob import ShadowCLOB
from research_logger import ResearchLogger
from strategy import CapitalFirstStrategy
from realistic_fill_simulator import RealisticFillSimulator
from feeds.market_book import PolymarketMarketFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


def prepare_fresh_data_dir():
    data_dir = Path(os.getenv("DATA_DIR", "/app/data")).expanduser()
    fresh = os.getenv("FRESH_START", "false").lower() in ("1", "true", "yes", "on")
    live_mode = os.getenv("LIVE_TRADING", "false").lower() == "true"
    if live_mode and fresh:
        raise RuntimeError("SAFETY LOCK: FRESH_START=true is forbidden with LIVE_TRADING=true")

    if str(data_dir) in ("/", ".", ""):
        raise RuntimeError(f"Refusing to wipe unsafe DATA_DIR={data_dir!r}")

    data_dir.mkdir(parents=True, exist_ok=True)
    if fresh:
        for child in data_dir.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    return data_dir


DATA = prepare_fresh_data_dir()

PAPER = os.getenv("PAPER_TRADING", "true").lower() == "true"
# SHADOW_CLOB is a hard safety override for non-authenticated Railway
# deployments.  If an old Railway service still has LIVE_TRADING=true,
# shadow mode wins and the live CLOB client is never imported/constructed.
SHADOW = os.getenv("SHADOW_CLOB", "false").lower() == "true"
LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"
if SHADOW and LIVE:
    log.warning("SHADOW_CLOB=true: forcing LIVE_TRADING=false; no authenticated CLOB client will be created")
    LIVE = False
EXECUTION_MODE = LIVE or SHADOW
if LIVE and PAPER:
    raise SystemExit("SAFETY LOCK: set PAPER_TRADING=false when LIVE_TRADING=true")
if not PAPER and not LIVE and not SHADOW:
    raise SystemExit("SAFETY LOCK: select PAPER_TRADING=true, LIVE_TRADING=true, or SHADOW_CLOB=true")
if EXECUTION_MODE and float(os.getenv("LIVE_BANKROLL_CAP", "100")) > 100.0:
    raise SystemExit("SAFETY LOCK: LIVE_BANKROLL_CAP cannot exceed $100")
if LIVE:
    from live_clob import LiveCLOB
    live = LiveCLOB()
else:
    live = None


# V15.2's strategy mechanics are calibrated around a $300 virtual capital
# frame. Live capital is a separate execution constraint and never changes
# that virtual frame.
STRATEGY_BANKROLL = 300.0 if EXECUTION_MODE else float(os.getenv("STARTING_CAPITAL", "300"))
LIVE_CAP = float(os.getenv("LIVE_BANKROLL_CAP", "100")) if EXECUTION_MODE else STRATEGY_BANKROLL
if STRATEGY_BANKROLL <= 0:
    raise SystemExit("SAFETY LOCK: STARTING_CAPITAL must be positive")
# V15.2 was calibrated/run with a $300 strategy capital ceiling.  Live
# capital is a separate execution constraint: scale the *dollar amount*
# produced by V15.2, without changing its price, band, side, cadence, or
# entry-state mechanics.
LIVE_SIZE_SCALE = min(
    1.0,
    float(os.getenv("LIVE_SIZE_SCALE", "0.3333333333333333"))
)
if EXECUTION_MODE and LIVE_SIZE_SCALE <= 0:
    raise SystemExit("SAFETY LOCK: LIVE_SIZE_SCALE must be positive")

strategy = CapitalFirstStrategy(
    bankroll=STRATEGY_BANKROLL,
    max_total_exposure=(300.0 if EXECUTION_MODE else float(os.getenv("MAX_TOTAL_EXPOSURE", "300"))),
    start_sec=float(os.getenv("START_TRADING_SECOND", "0")),
    stop_sec=float(os.getenv("STOP_TRADING_SECOND", "240")),
    hard_cutoff_seconds=float(os.getenv("HARD_CUTOFF_SECONDS", "60")),
    # Zero here because the trader's median 2s cadence is not a hard rule.
    min_trade_gap_seconds=float(os.getenv("MIN_TRADE_GAP_SECONDS", "0")),
)

if EXECUTION_MODE:
    ledger = LiveLedger(DATA / ("live_state.json" if LIVE else "shadow_state.json"), min(strategy.bankroll, LIVE_CAP))
    risk = LiveRisk(DATA / ("live_risk.json" if LIVE else "shadow_risk.json"))
    execution_queue = LiveExecutionQueue(DATA / ("live_execution_queue.json" if LIVE else "shadow_execution_queue.json"))
    if SHADOW:
        live = ShadowCLOB(DATA / "shadow_clob.json")
else:
    ledger = PaperLedger(DATA / "paper_state.json", strategy.bankroll)
    risk = None
    execution_queue = None

# Paper mode intentionally runs TWO ledgers in parallel from the exact same
# strategy signals:
#   1) `ledger`: the legacy instant-fill benchmark (signal == fill).
#   2) `realistic_ledger`: only actual simulated resting-order fills enter it.
# The benchmark is retained so the realism gap is measurable rather than hidden.
realistic_fill_enabled = PAPER and os.getenv("REALISTIC_FILL_SIM", "true").lower() in ("1", "true", "yes", "on")
if realistic_fill_enabled:
    realistic_ledger = PaperLedger(DATA / "realistic_fill_state.json", strategy.bankroll)
    realistic_feed = PolymarketMarketFeed(
        url=os.getenv("MARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
        reconnect_seconds=float(os.getenv("MARKET_WS_RECONNECT_SECONDS", "2")),
    )
    realistic_simulator = RealisticFillSimulator(
        DATA / "realistic_fill_orders.json",
        websocket_feed=realistic_feed,
    )
else:
    realistic_ledger = None
    realistic_feed = None
    realistic_simulator = None

# The ledger file is created here so startup validation never treats an
# expected first-run artifact as a fatal missing dependency.
ledger.save()
if realistic_ledger is not None:
    realistic_ledger.save()
research = ResearchLogger(DATA, ledger)

markets = {}
histories = {}
pending = {}
last_disc = 0.0
last_report = 0.0
last_maintenance = 0.0
last_heartbeat = 0.0
last_trade = {}
ob_last = {}
decision_last = {}
consecutive_errors = 0
# Global trader-style trade clock. This is the execution cadence model,
# not a minimum-gap rule. One accepted trade advances this clock by a
# sample from the trader's observed intertrade distribution.
next_trade_at = 0.0
scan_offset = 0
current_target_band = None
adaptive_planner = CLOBAdaptivePlanner(
    max_order=float(os.getenv("MAX_SINGLE_ORDER", "5")),
    batch_window_seconds=float(os.getenv("ADAPTIVE_BATCH_WINDOW_SECONDS", "6")),
)

# P90 of the observed trader intertrade distribution. This is recorded as a
# descriptive burst boundary only; it is NOT used as a trade trigger.
BURST_GAP_SECONDS = float(os.getenv("BURST_GAP_SECONDS", "18"))


def asset_exposure(asset):
    return sum(
        float(p.get("cost", 0))
        for p in ledger.positions.values()
        if p.get("asset") == asset
    )


def prepare_histories(history_map, now, window_seconds=60.0):
    for side in ("Up", "Down"):
        history_map[side] = [
            point for point in history_map.get(side, [])
            if float(point[0]) >= now - window_seconds
        ]


def market_entry_state(condition, now):
    entries = [
        t for t in ledger.trades
        if t.get("action") == "BUY"
        and t.get("condition") == condition
    ]
    if not entries:
        return {
            "count": 0,
            "seconds_since_first": 0.0,
            "seconds_since_previous": None,
            "side": None,
            "price": None,
            "burst_position": 0,
        }

    ordered = sorted(entries, key=lambda t: float(t.get("ts", now)))
    first_ts = float(ordered[0].get("ts", now))
    previous_ts = float(ordered[-1].get("ts", now))
    gaps = [float(cur.get("ts", now)) - float(prev.get("ts", now))
            for prev, cur in zip(ordered, ordered[1:])]

    burst_position = 1
    for gap in reversed(gaps):
        if gap <= BURST_GAP_SECONDS:
            burst_position += 1
        else:
            break

    latest = ordered[-1]
    return {
        "count": len(ordered),
        "seconds_since_first": max(0.0, now - first_ts),
        "seconds_since_previous": max(0.0, now - previous_ts),
        "side": latest.get("side"),
        "price": latest.get("price"),
        "burst_position": burst_position,
    }


def p(message):
    log.info(message)


def startup_data_check():
    required = [
        "decisions.jsonl",
        "orderbooks.jsonl",
        "trades.csv",
        "markets.csv",
        "resolutions.csv",
        "pnl_1min.csv",
        "paper_state.json" if not EXECUTION_MODE else ("live_state.json" if LIVE else "shadow_state.json"),
    ]
    if realistic_ledger is not None:
        required.extend([
            "realistic_fill_state.json", "realistic_fill_orders.json",
            "realistic_orders.csv", "realistic_fills.csv",
            "realistic_unfilled.csv", "realistic_settlement_details.csv",
            "realistic_fill_metrics_1min.csv", "realistic_fill_regime_1min.csv",
            "realistic_fill_band_1min.csv",
        ])
    missing = [name for name in required if not (DATA / name).exists()]
    if missing:
        raise RuntimeError(f"DATA STORE INITIALIZATION FAILED: {missing}")


def resolve_pending(now):
    for condition, market in list(pending.items()):
        if now < float(market.get("end_ts", 0)) + 2:
            continue

        try:
            token, outcome, status = resolve(market)
            if token:
                closed = ledger.settle(condition, token)
                pnl = sum(float(x["pnl"]) for x in closed)
                realistic_closed = realistic_ledger.settle(condition, token) if realistic_ledger is not None else []
                realistic_pnl = sum(float(x["pnl"]) for x in realistic_closed)

                research.record_resolution(
                    ts=now,
                    market=market,
                    winner=outcome or token,
                    winner_token=token,
                    closed=closed,
                )

                p(
                    f"RESOLUTION | asset={market['asset']} | slug={market['slug']} "
                    f"| winner={outcome or token} | pnl={pnl:+.4f} | closed={len(closed)}"
                )
                if realistic_ledger is not None:
                    research.record_realistic_resolution(
                        ts=now, market=market, winner=outcome or token,
                        winner_token=token, closed=realistic_closed,
                    )
                    p(
                        f"REALISTIC RESOLUTION | asset={market['asset']} | slug={market['slug']} "
                        f"| winner={outcome or token} | pnl={realistic_pnl:+.4f} | filled_closed={len(realistic_closed)}"
                    )

                pending.pop(condition, None)
                markets.pop(condition, None)
                histories.pop(condition, None)
            elif status == "CLOSED_UNRESOLVED":
                research.record_resolution_error(
                    ts=now, market=market, status=status
                )
        except Exception as exc:
            research.record_resolution_error(
                ts=now,
                market=market,
                status=f"ERROR:{type(exc).__name__}",
            )
            p(
                f"RESOLUTION ERROR | {market['slug']} | "
                f"{type(exc).__name__}: {exc}"
            )


def report(books):
    global last_report

    now = time.time()
    interval = float(os.getenv("REPORT_INTERVAL_SECONDS", "60"))
    if now - last_report < interval:
        return

    last_report = now
    if EXECUTION_MODE:
        metrics = {"cash":ledger.cash,"open_cost":ledger.total_open_cost(),"market_value":ledger.equity(books)-ledger.cash,"unrealized":ledger.equity(books)-ledger.cash-ledger.total_open_cost(),"realized":ledger.realized,"equity":ledger.equity(books),"pnl":ledger.equity(books)-ledger.initial_cash,"drawdown":0.0,"marked":len(ledger.positions)}
    else:
        metrics = ledger.mark(books)
    metrics["positions"] = len(ledger.positions)
    research.record_pnl(now, metrics)

    p(
        f"P&L ours ${metrics['pnl']:+.2f} | realized ${metrics['realized']:+.2f} "
        f"| unrealized ${metrics['unrealized']:+.2f} | cash ${metrics['cash']:.2f} "
        f"| open ${metrics['open_cost']:.2f} | positions {metrics['positions']}"
    )
    if realistic_ledger is not None:
        rmetrics = realistic_ledger.mark(books)
        sim = realistic_simulator.metrics(now)
        research.record_realistic_metrics(now, rmetrics, sim, instant_metrics=metrics)
        p(
            f"PAPER REALISTIC | pnl=${rmetrics['pnl']:+.2f} | realized=${rmetrics['realized']:+.2f} "
            f"| unrealized=${rmetrics['unrealized']:+.2f} | cash=${rmetrics['cash']:.2f} "
            f"| open=${rmetrics['open_cost']:.2f} | positions={len(realistic_ledger.positions)} "
            f"| signals={sim['orders']} | filled={sim['filled']} | partial={sim['partial']} "
            f"| pending={sim['pending']} | expired={sim['expired_unfilled']} "
            f"| fill_rate={sim['filled']/sim['orders']:.2%}" if sim['orders'] else
            f"PAPER REALISTIC | pnl=${rmetrics['pnl']:+.2f} | realized=${rmetrics['realized']:+.2f} "
            f"| unrealized=${rmetrics['unrealized']:+.2f} | cash=${rmetrics['cash']:.2f} "
            f"| open=${rmetrics['open_cost']:.2f} | positions={len(realistic_ledger.positions)} | signals=0 | filled=0 | pending=0 | expired=0 | fill_rate=0.00%"
        )


def main():
    global last_disc, last_maintenance, last_heartbeat, consecutive_errors, next_trade_at, scan_offset, current_target_band

    startup_data_check()
    if realistic_simulator is not None:
        # Recover any simulator fills that were durably recorded before a process
        # crash but were not yet written to realistic_fill_state.json.
        for recovery in realistic_simulator.reconcile_ledger(realistic_ledger):
            recovery_meta = dict(recovery.get("meta") or {})
            realistic_ledger.buy(
                condition=recovery["condition"], token=recovery["token"], market=recovery["market"],
                side=recovery["side"], price=recovery["price"], notional=recovery["notional"],
                ts=recovery["fill_ts"], meta={**recovery_meta, "sim_order_id": recovery["order_id"], "realistic_fill_recovery": True},
            )
            p(
                f"REALISTIC FILL RECOVERY | order_id={recovery['order_id']} "
                f"| shares={recovery['shares']:.6f} | cost=${recovery['notional']:.4f} "
                f"| price=${recovery['price']:.4f}"
            )
        # Re-subscribe to any surviving pending orders before the websocket starts.
        for order in realistic_simulator.pending():
            realistic_simulator.subscribe_token(order["token"])
        realistic_simulator.start()
        p("REALISTIC FILL SIM READY | resting_maker=true | instant_benchmark=true | websocket=public_market_channel")
    if EXECUTION_MODE:
        info = live.preflight()
        if SHADOW:
            p(f"SHADOW CLOB READY | geo={info.get('country','UNKNOWN')}/{info.get('region','')} | public_data_only=true | real_orders=DISABLED")
        # Reconcile every locally-known active order against the authoritative
        # CLOB order endpoint before allowing a new order. A restart must never
        # guess whether an old order filled or canceled.
        active_local = [
            oid for oid, rec in ledger.orders.items()
            if rec.get("status") in {"SUBMITTED", "OPEN", "PARTIAL", "LIVE", "MATCHED"}
        ]
        if active_local:
            states = live.reconcile_orders(active_local)
            ledger.reconcile_orders(states)

        open_orders = live.get_open_orders()
        unknown = []
        for order in open_orders:
            oid = str(order.get("id") or order.get("orderID") or order.get("orderId") or "")
            if oid and oid not in ledger.orders:
                unknown.append(oid)
        if unknown:
            try:
                live.cancel_all()
            except Exception as exc:
                raise RuntimeError(
                    "LIVE STARTUP HALT: unknown open orders exist and emergency cancel failed: "
                    + ",".join(unknown[:10]) + f" | {type(exc).__name__}: {exc}"
                ) from exc
            raise RuntimeError(
                "LIVE STARTUP HALT: unknown open orders existed; they were canceled: "
                + ",".join(unknown[:10])
            )

        ledger.open_order_reserve(open_orders)
        ledger.sync_trades(live.get_trades())
        risk.sync_realized(ledger.realized)
        if risk.halted:
            try:
                live.cancel_all()
            finally:
                raise RuntimeError(f"LIVE STARTUP HALT: {risk.halt_reason}")
        p(
            f"LIVE PREFLIGHT OK | signer={info['signer']} | funder={info['funder']} "
            f"| signature_type={info['signature_type']} | collateral=${info['balance']:.4f} "
            f"| allowance=${info['allowance']:.4f} | geo={info['country']}/{info['region']}"
        )
        if os.getenv("PREFLIGHT_ONLY", "false").strip().lower() == "true":
            p("PREFLIGHT ONLY | no orders will be submitted")
            return
    p("BOT B | LIVE $100 CAPITAL | V15.2 40PCT EXACT DISTRIBUTION" if LIVE else ("BOT B | SHADOW CLOB | $100 CAPITAL | V15.2 40PCT EXACT DISTRIBUTION" if SHADOW else "BOT B | PAPER ONLY | V15.2 40PCT EXACT DISTRIBUTION"))

    books = {}
    while True:
        try:
            now = time.time()

            if realistic_simulator is not None:
                for expired_order in realistic_simulator.expire(now):
                    research.record_realistic_unfilled(expired_order, "EXPIRED_UNFILLED")
                    p(
                        f"REALISTIC UNFILLED | asset={expired_order['meta'].get('asset','')} "
                        f"| side={expired_order['side']} | target=${expired_order['target_price']:.4f} "
                        f"| notional=${expired_order['notional']:.4f} | reason=EXPIRED_UNFILLED "
                        f"| elapsed={max(0.0, now-expired_order['placed_ts']):.1f}s"
                    )
                for fill in realistic_simulator.drain_fills():
                    market = markets.get(fill.get("condition"))
                    if market is None:
                        meta = fill.get("meta") or {}
                        market = {
                            "id": meta.get("market_id", fill.get("condition", "")),
                            "condition": fill.get("condition"), "slug": meta.get("slug", ""),
                            "asset": meta.get("asset", ""), "market": fill.get("market", ""),
                            "start_ts": meta.get("start_ts", fill.get("placed_ts", now)),
                            "end_ts": meta.get("end_ts", now),
                            "up": meta.get("up_token", ""), "down": meta.get("down_token", ""),
                        }
                    fill_meta = dict(fill.get("meta") or {})
                    fill_meta.update({
                        "realistic_fill": True,
                        "fill_source": "CLOB_WS_LAST_TRADE",
                        "fill_latency_s": fill.get("fill_latency_s"),
                        "target_price": fill.get("target_price"),
                        "depth_ahead": fill.get("depth_ahead"),
                        "queue_volume": fill.get("cumulative_volume_through_price"),
                        "sim_order_id": fill.get("order_id"),
                    })
                    trade = realistic_ledger.buy(
                        condition=fill["condition"], token=fill["token"], market=fill["market"],
                        side=fill["side"], price=fill["price"], notional=fill["notional"],
                        ts=fill["fill_ts"], meta=fill_meta,
                    )
                    research.record_realistic_fill(fill, trade, market)
                    p(
                        f"REALISTIC FILL | asset={market.get('asset','')} | side={fill['side']} "
                        f"| target=${fill['target_price']:.4f} | fill=${fill['price']:.4f} "
                        f"| shares={fill['shares']:.6f} | cost=${fill['notional']:.4f} "
                        f"| latency={fill.get('fill_latency_s')!s}s | status={fill['status']}"
                    )

            if now - last_disc >= 20:
                for market in discover():
                    markets[market["condition"]] = market

                for condition, market in list(markets.items()):
                    if any(
                        position.get("condition") == condition
                        for position in ledger.positions.values()
                    ):
                        pending[condition] = market
                    elif market["end_ts"] < now - 30:
                        markets.pop(condition, None)

                last_disc = now
                p(
                    f"MARKETS | active={len(markets)} "
                    f"| pending_resolution={len(pending)}"
                )

            if EXECUTION_MODE:
                try:
                    fills = ledger.sync_trades(live.get_trades())
                    risk.sync_realized(ledger.realized)
                    if risk.halted:
                        try:
                            live.cancel_all()
                        finally:
                            raise RuntimeError(f"LIVE RISK HALT: {risk.halt_reason}")
                    for fill in fills:
                        fill_market = markets.get(fill.get("condition"))
                        if fill_market:
                            fill_elapsed = max(0.0, now - float(fill_market["start_ts"]))
                            fill_left = max(0.0, float(fill_market["end_ts"]) - now)
                            try:
                                research.record_trade(
                                    ts=now, market=fill_market, elapsed=fill_elapsed, left=fill_left,
                                    up_bid=books.get(fill_market["up"]), up_ask=None,
                                    up_depth=0.0, down_bid=books.get(fill_market["down"]), down_ask=None,
                                    down_depth=0.0, trade=fill, score=fill.get("trajectory_likelihood"),
                                    momentum=None, reason=fill.get("signal_reason"),
                                    cash_after=ledger.cash, exposure_after=ledger.total_open_cost(),
                                    entry_count_before=fill.get("entry_count_before", 0),
                                    burst_position=fill.get("burst_position", 0),
                                    seconds_since_previous=fill.get("seconds_since_previous_trade"),
                                    up_history=histories.get(fill.get("condition"), {}).get("Up", []),
                                    down_history=histories.get(fill.get("condition"), {}).get("Down", []),
                                )
                            except Exception as log_exc:
                                raise RuntimeError(f"LIVE RESEARCH LOGGING HALT: {log_exc}") from log_exc
                        executed_band, _ = strategy.fine_band(float(fill["price"]))
                        if executed_band is None:
                            raise RuntimeError(f"LIVE FILL PRICE OUTSIDE FINE BANDS: {fill['price']!r}")
                        observed_notional = (
                            float(fill["notional"]) / LIVE_SIZE_SCALE
                            if LIVE_SIZE_SCALE > 0 else float(fill["notional"])
                        )
                        strategy.observe_trade_distribution(executed_band, observed_notional)
                        last_trade[fill.get("condition")] = now
                        p(
                            f"LIVE FILL | trade_id={fill['trade_id']} | order_id={fill['order_id']} "
                            f"| side={fill['side']} | price=${fill['price']:.6f} "
                            f"| shares={fill['shares']:.6f} | cost=${fill['notional']:.4f}"
                        )
                    # Heartbeat keeps authenticated live orders alive.
                    if now - last_heartbeat >= float(os.getenv("HEARTBEAT_SECONDS", "5")):
                        live.heartbeat(); last_heartbeat = now
                except Exception as exc:
                    p(f"LIVE SYNC/HEARTBEAT ERROR | {type(exc).__name__}: {exc}")
                    risk.halt(f"LIVE_SYNC_ERROR:{type(exc).__name__}")
                    try: live.cancel_all()
                    except Exception: pass
                    raise
            if EXECUTION_MODE:
                # Queued allocations belong to their original 5-minute market.
                # They cannot safely be moved to another market after cutoff.
                for condition, market in list(markets.items()):
                    if market.get("end_ts", 0) - now <= strategy.hard_cutoff_seconds:
                        expired = execution_queue.expire_condition(condition, "MARKET_CUTOFF")
                        if expired > 0:
                            p(f"LIVE QUEUE EXPIRED | asset={market.get('asset')} | queued=${expired:.4f} | reason=MARKET_CUTOFF")

            # Cancel our outstanding orders before the strategy cutoff. Never leave
            # a passive order resting into resolution when the model has stopped trading.
            if EXECUTION_MODE:
                for condition, market in list(markets.items()):
                    if market.get("end_ts", 0) - now <= strategy.hard_cutoff_seconds:
                        try:
                            live.cancel_market_orders(condition)
                        except Exception as exc:
                            p(f"LIVE CANCEL ERROR | {market.get('slug')} | {type(exc).__name__}: {exc}")
                            risk.halt(f"CANCEL_ERROR:{type(exc).__name__}")
                            try: live.cancel_all()
                            except Exception: pass
                            raise
            resolve_pending(now)
            books = {}

            if now >= next_trade_at and current_target_band is None:
                current_target_band = None
            trade_taken_this_loop = False
            market_list = list(markets.values())
            if market_list:
                scan_offset %= len(market_list)
                market_list = market_list[scan_offset:] + market_list[:scan_offset]

            for market in market_list:
                if not market.get("end_ts") or market["end_ts"] < now - 30:
                    continue

                elapsed = now - market["start_ts"]
                left = market["end_ts"] - now

                if left <= 0 or elapsed < 0 or elapsed > 300:
                    continue

                try:
                    up_bid, up_ask, up_bid_depth, up_ask_depth = book(market["up"])
                    down_bid, down_ask, down_bid_depth, down_ask_depth = book(market["down"])
                except Exception as exc:
                    p(
                        f"BOOK ERROR | {market['asset']} | {market['slug']} "
                        f"| {type(exc).__name__}: {exc}"
                    )
                    continue

                books[market["up"]] = up_bid
                books[market["down"]] = down_bid

                history = histories.setdefault(
                    market["condition"], {"Up": [], "Down": []}
                )

                if up_bid is not None:
                    history["Up"].append((now, up_bid))
                if down_bid is not None:
                    history["Down"].append((now, down_bid))
                prepare_histories(history, now, 60.0)

                orderbook_interval = float(
                    os.getenv("ORDERBOOK_SAMPLE_SECONDS", "1")
                )
                if (
                    now - ob_last.get(market["condition"], 0)
                    >= orderbook_interval
                ):
                    research.record_orderbook(
                        ts=now,
                        market=market,
                        elapsed=elapsed,
                        left=left,
                        up_bid=up_bid,
                        up_ask=up_ask,
                        up_depth=up_bid_depth,
                        down_bid=down_bid,
                        down_ask=down_ask,
                        down_depth=down_bid_depth,
                        up_ask_depth=up_ask_depth,
                        down_ask_depth=down_ask_depth,
                        up_history=history["Up"],
                        down_history=history["Down"],
                    )
                    ob_last[market["condition"]] = now

                # Discovery is fail-closed: missing acceptingOrders must never
                # be interpreted as permission to trade.
                if market.get("accepting_orders") is not True:
                    continue

                # The reference trader's intertrade cadence is global across
                # markets. Prevent the bot from placing one trade in every
                # market on every one-second loop.
                if now < next_trade_at:
                    continue

                exposure = ledger.exposure(market["condition"])
                total_exp = ledger.total_open_cost()
                state = market_entry_state(market["condition"], now)

                # In live mode the strategy sees the same $300 virtual capital
                # frame it was designed/calibrated for. The live risk engine
                # independently constrains the actual live execution budget.
                virtual_scale = LIVE_SIZE_SCALE if EXECUTION_MODE else 1.0
                strategy_cash = ledger.cash / virtual_scale if virtual_scale > 0 else 0.0
                strategy_total_exposure = (
                    ledger.total_open_cost() / virtual_scale
                    if virtual_scale > 0 else ledger.total_open_cost()
                )
                signal = strategy.decide(
                    elapsed,
                    up_ask,
                    down_ask,
                    up_bid,
                    down_bid,
                    history["Up"],
                    history["Down"],
                    exposure / virtual_scale if virtual_scale > 0 else exposure,
                    strategy_cash,
                    up_depth=up_bid_depth,
                    down_depth=down_bid_depth,
                    now=now,
                    total_exposure=strategy_total_exposure,
                    market_entry_count=state["count"],
                    seconds_since_first_entry=state["seconds_since_first"],
                    thesis_side=state["side"],
                    thesis_price=state["price"],
                    asset=market["asset"],
                    market=market["asset"],
                    process_target_band=current_target_band,
                )

                decision_interval = float(
                    os.getenv("DECISION_SAMPLE_SECONDS", "1")
                )
                if (
                    signal is not None
                    or now - decision_last.get(market["condition"], 0)
                    >= decision_interval
                ):
                    research.record_decision(
                        ts=now,
                        market=market,
                        elapsed=elapsed,
                        left=left,
                        up_bid=up_bid,
                        up_ask=up_ask,
                        up_depth=up_bid_depth,
                        down_bid=down_bid,
                        down_ask=down_ask,
                        down_depth=down_bid_depth,
                        signal=signal,
                        exposure=exposure,
                        cash=ledger.cash,
                        entry_count=state["count"],
                        burst_position=state["burst_position"],
                        seconds_since_previous=state["seconds_since_previous"],
                        up_history=history["Up"],
                        down_history=history["Down"],
                    )
                    decision_last[market["condition"]] = now

                if signal is None:
                    continue

                if left <= strategy.hard_cutoff_seconds:
                    continue

                if (
                    strategy.min_trade_gap_seconds
                    and now - last_trade.get(market["condition"], 0)
                    < strategy.min_trade_gap_seconds
                ):
                    continue

                token = market["up"] if signal.side == "Up" else market["down"]

                # V14: 40% trader-notional target; $300 is the only
                # experiment-level paper exposure ceiling.
                target = strategy.entry_target(
                    signal.price,
                    market["asset"],
                    state["count"],
                )
                # Keep V15.2's signal mechanics intact. In live mode only,
                # scale the dollar amount to the actual live bankroll relative
                # to the strategy's $300 design capital. Price, side, market,
                # cadence, band selection and entry state are untouched.
                #
                # IMPORTANT: never clip a live signal to current cash/exposure.
                # Clipping silently changes the strategy's intended allocation.
                # Capital is checked at execution time after compatible queue
                # allocations have been assembled.
                raw_notion = float(signal.notional)
                capital_scale = LIVE_SIZE_SCALE if EXECUTION_MODE else 1.0
                notion = raw_notion * capital_scale
                if EXECUTION_MODE and notion > risk.max_order + 1e-9:
                    p(
                        f"LIVE SIGNAL REJECTED | asset={market['asset']} | side={signal.side} "
                        f"| intended=${notion:.4f} | max_single=${risk.max_order:.4f} "
                        f"| reason=UNEXPECTED_LIVE_SCALE"
                    )
                    continue

                min_paper_order = float(os.getenv("MIN_PAPER_FILL_USD", "0.10"))
                if not EXECUTION_MODE and notion < min_paper_order:
                    continue

                band, regime = strategy.fine_band(signal.price)

                meta = {
                    "slug": market["slug"],
                    "asset": market["asset"],
                    "start_ts": market["start_ts"],
                    "end_ts": market["end_ts"],
                    "market_id": market["id"],
                    "up_token": market["up"],
                    "down_token": market["down"],
                    "model_version": strategy.VERSION,
                    "entry_count_before": state["count"],
                    "burst_position": state["burst_position"],
                    "seconds_since_first_entry": state["seconds_since_first"],
                    "seconds_since_previous_trade": state["seconds_since_previous"],
                    "regime": regime,
                    "fine_band": band,
                    "execution_mode": "PASSIVE_BID_PROXY",
                    "target_capital": target,
                    "raw_signal_notional": raw_notion,
                    "live_size_scale": capital_scale,
                    "bid_size": up_bid_depth if signal.side == "Up" else down_bid_depth,
                    "trajectory_likelihood": signal.score,
                }

                live_order_submitted = False
                executed_order = None

                if EXECUTION_MODE:
                    # Persist the signal exactly once. The adaptive executor may
                    # combine several live signals into one CLOB FAK order, but it
                    # never lets the execution price exceed any constituent
                    # signal's regime-specific price ceiling.
                    signal_key = (
                        f"{market['condition']}|{token}|{signal.side}|{float(signal.price):.10f}|"
                        f"{state['count']}|{state['burst_position']}|{raw_notion:.10f}"
                    )
                    meta["signal_key"] = signal_key
                    meta["execution_mode"] = "CLOB_ADAPTIVE_FAK"
                    try:
                        signal_tick = float(live.tick_size(token))
                    except Exception:
                        signal_tick = float(os.getenv("DEFAULT_TICK_SIZE", "0.01"))
                    meta["max_execution_price"] = adaptive_planner.max_price(signal.price, signal_tick, regime)
                    meta["adaptive_regime"] = regime

                    try:
                        if not execution_queue.has_pending_signal(
                            condition=market["condition"], token=token, side=signal.side,
                            price=signal.price, signal_key=signal_key
                        ):
                            execution_queue.enqueue(
                                condition=market["condition"], token=token, side=signal.side,
                                market=market["market"], price=signal.price, notional=notion, meta=meta,
                            )
                    except Exception as exc:
                        risk.halt(f"QUEUE_ERROR:{type(exc).__name__}")
                        try: live.cancel_all()
                        except Exception: pass
                        raise RuntimeError(f"LIVE QUEUE PATH HALT: {exc}") from exc

                    # Reconcile authoritative order/trade state before allocating
                    # another CLOB lot. FAK orders are taker executions by design.
                    try:
                        open_orders = live.get_open_orders()
                        active_ids = [str(oid) for oid, rec in ledger.orders.items()
                                      if rec.get("status") in {"SUBMITTED", "OPEN", "PARTIAL", "LIVE", "MATCHED"}]
                        if active_ids:
                            ledger.reconcile_orders(live.reconcile_orders(active_ids))
                            open_orders = live.get_open_orders()
                        unknown_ids = [str(o.get("id") or o.get("orderID") or o.get("orderId") or "")
                                       for o in open_orders
                                       if str(o.get("id") or o.get("orderID") or o.get("orderId") or "")
                                       and str(o.get("id") or o.get("orderID") or o.get("orderId") or "") not in ledger.orders]
                        if unknown_ids:
                            try: live.cancel_all()
                            finally: risk.halt("UNKNOWN_OPEN_ORDERS")
                            raise RuntimeError("LIVE ORDER STATE HALT: unknown open orders " + ",".join(unknown_ids[:10]))
                        local_reserved = ledger.total_reserved()
                    except Exception as exc:
                        if not risk.halted:
                            risk.halt(f"ORDER_STATE_ERROR:{type(exc).__name__}")
                        try: live.cancel_all()
                        except Exception: pass
                        raise RuntimeError(f"LIVE ORDER STATE ERROR | {type(exc).__name__}: {exc}") from exc

                    try:
                        min_cost, min_shares = live.minimum_order(token, market["condition"], signal.price)
                        current_ask = up_ask if token == market["up"] else down_ask
                        try:
                            tick = float(live.tick_size(token))
                        except Exception:
                            tick = float(os.getenv("DEFAULT_TICK_SIZE", "0.01"))
                        # Ask is the actual execution reference; only proceed when
                        # this current ask is accepted by at least one queued signal.
                        group_items = [x for x in execution_queue.pending()
                                       if str(x.get("condition")) == str(market["condition"])
                                       and str(x.get("token")) == str(token)
                                       and str(x.get("side")) == str(signal.side)]
                        plan = adaptive_planner.plan(
                            group_items, current_ask=current_ask, min_shares=min_shares,
                            tick_size=tick, now=now
                        )
                        if plan is None:
                            p(f"CLOB ADAPTIVE WAIT | asset={market['asset']} | side={signal.side} "
                              f"| signal=${notion:.4f} | bid=${signal.price:.4f} | ask={current_ask!r} "
                              f"| reason=NO_VALID_BATCH")
                            continue

                        # Risk is measured on the worst-case limit cost plus the
                        # configured taker-fee estimate. Actual fills can be cheaper.
                        fee_rate = float(os.getenv("TAKER_FEE_RATE", "0.07"))
                        fee_est = plan.order_shares * fee_rate * plan.execution_price * (1.0 - plan.execution_price)
                        required_cash = plan.requested_budget + fee_est
                        market_reserved = ledger.reserved_for_condition(market["condition"], open_orders)
                        available_cash = max(0.0, ledger.cash - local_reserved)
                        if required_cash > available_cash + 1e-9:
                            p(f"CLOB ADAPTIVE CAPITAL WAIT | asset={market['asset']} | required=${required_cash:.4f} | available=${available_cash:.4f}")
                            continue
                        total_exposure = ledger.total_open_cost() + local_reserved
                        if total_exposure + plan.requested_budget > risk.max_total + 1e-9:
                            continue
                        if ledger.exposure(market["condition"]) + market_reserved + plan.requested_budget > risk.max_market + 1e-9:
                            continue
                        ok, why = risk.authorize(
                            plan.requested_budget, total_exposure,
                            ledger.exposure(market["condition"]) + market_reserved,
                            len(open_orders), ledger.cash, reserved=local_reserved
                        )
                        if not ok:
                            p(f"CLOB ADAPTIVE RISK BLOCK | {why} | asset={market['asset']}")
                            continue

                        response = live.adaptive_buy(
                            token, plan.execution_price, plan.order_shares, market["condition"]
                        )
                        order_id = str(response.get("orderID") or response.get("orderId") or response.get("id") or "")
                        if not order_id:
                            raise RuntimeError(f"CLOB adaptive accepted call without order id: {response!r}")
                        selected_meta = dict((plan.items[0].get("meta") or {}) if plan.items else meta)
                        selected_meta.update({
                            "execution_mode": "CLOB_ADAPTIVE_FAK",
                            "execution_price": plan.execution_price,
                            "requested_budget": plan.requested_budget,
                            "execution_topup": plan.topup,
                            "selected_signal_count": len(plan.items),
                            "max_execution_price": plan.max_execution_price,
                        })
                        ledger.record_order(
                            order_id, market["condition"], token, signal.side,
                            plan.execution_price, plan.requested_budget, market["market"], meta=selected_meta
                        )
                        execution_queue.mark_submitted_group(
                            {"items": list(plan.items)}, plan.requested_budget if plan.requested_budget <= sum(float(x.get("notional",0)) for x in plan.items)+1e-9 else sum(float(x.get("notional",0)) for x in plan.items), order_id
                        )
                        live_order_submitted = True
                        executed_order = {"order_id": order_id, "plan": plan, "response": response}
                        p(f"LIVE ORDER SUBMITTED | mode=CLOB_ADAPTIVE_FAK | order_id={order_id} | asset={market['asset']} "
                          f"| side={signal.side} | limit=${plan.execution_price:.4f} | budget=${plan.requested_budget:.4f} "
                          f"| shares={plan.order_shares:.6f} | signals={len(plan.items)} | topup=${plan.topup:.4f}")
                    except Exception as exc:
                        msg = str(exc).lower()
                        if any(x in msg for x in ("below market minimum", "market minimum", "no acceptable ask")):
                            p(f"CLOB ADAPTIVE WAIT | asset={market['asset']} | reason={exc}")
                            continue
                        risk.halt(f"ADAPTIVE_ORDER_ERROR:{type(exc).__name__}")
                        try: live.cancel_all()
                        except Exception: pass
                        raise RuntimeError(f"LIVE ADAPTIVE ORDER HALT: {exc}") from exc

                else:
                    trade = ledger.buy(market["condition"], token, market["market"], signal.side, signal.price, notion, now, meta)
                    if realistic_simulator is not None:
                        depth_ahead = up_bid_depth if signal.side == "Up" else down_bid_depth
                        # Use the displayed size exactly at the signal price when
                        # available. The regular book() result is the best-bid size
                        # and is normally identical, but an explicit target-level
                        # lookup avoids silently treating another price level as
                        # queue position. This remains only an approximation of
                        # real exchange queue priority.
                        try:
                            depth_ahead = book_depth_at_price(token, signal.price)
                        except Exception:
                            depth_ahead = up_bid_depth if signal.side == "Up" else down_bid_depth
                        realistic_order = realistic_simulator.place_order(
                            condition=market["condition"], market=market["market"], token=token,
                            side=signal.side, target_price=signal.price, notional=notion,
                            placed_ts=now, window_end_ts=market["end_ts"],
                            depth_ahead=depth_ahead, meta=meta,
                        )
                        research.record_realistic_order(
                            ts=now, market=market, signal=signal, notional=notion,
                            target_price=signal.price, depth_ahead=depth_ahead, meta=meta,
                            order_id=realistic_order["id"],
                        )

                if EXECUTION_MODE and not live_order_submitted:
                    # Queueing is not an accepted trade. Do not advance the global
                    # trader cadence, rotate the scan, or log a live accepted trade
                    # until an actual CLOB order has been submitted.
                    continue

                pending[market["condition"]] = market
                last_trade[market["condition"]] = now

                if EXECUTION_MODE:
                    p(
                        f"TRADE SIGNAL ACCEPTED | V15.2 40PCT | asset={market['asset']} "
                        f"| side={signal.side} | notional=${notion:.2f} "
                        f"| raw_v15.2=${raw_notion:.2f} | scale={capital_scale:.6f} "
                        f"| bid=${signal.price:.4f} | target=${target:.2f} "
                        f"| entry_count={state['count']} | burst={state['burst_position']} "
                        f"| {signal.reason}"
                    )
                else:
                    p(
                        f"TRADE PAPER | V15.2 40PCT | asset={market['asset']} "
                        f"| side={signal.side} | notional=${notion:.2f} "
                        f"| raw_v15.2=${raw_notion:.2f} | scale={capital_scale:.6f} "
                        f"| bid=${signal.price:.4f} | target=${target:.2f} "
                        f"| entry_count={state['count']} | burst={state['burst_position']} "
                        f"| {signal.reason}"
                    )
                    research.record_trade(
                        ts=now, market=market, elapsed=elapsed, left=left,
                        up_bid=up_bid, up_ask=up_ask, up_depth=up_bid_depth,
                        down_bid=down_bid, down_ask=down_ask, down_depth=down_bid_depth,
                        trade=trade, score=signal.score, momentum=None, reason=signal.reason,
                        cash_after=ledger.cash, exposure_after=ledger.exposure(market["condition"]),
                        entry_count_before=state["count"], burst_position=state["burst_position"],
                        seconds_since_previous=state["seconds_since_previous"],
                        up_history=history["Up"], down_history=history["Down"],
                    )
                    executed_band, _ = strategy.fine_band(float(signal.price))
                    if executed_band is None:
                        raise RuntimeError(
                            f"cannot classify executed trade price {signal.price!r} into a fine band"
                        )
                    strategy.observe_trade_distribution(executed_band, notion)
                    ledger.save()

                current_target_band = None
                trade_taken_this_loop = True

                # Advance the single global trader-style execution clock.
                next_trade_at = now + max(0.0, strategy.cadence.sample_gap())
                scan_offset += 1

                # At most one accepted trade per outer loop. A zero-second
                # empirical gap is handled on the next 250ms loop rather than
                # creating multiple same-timestamp market entries.
                if trade_taken_this_loop:
                    break

            report(books)

            maintenance_interval = float(
                os.getenv("DATA_MAINTENANCE_SECONDS", "3600")
            )
            if now - last_maintenance >= maintenance_interval:
                research.maintenance()
                last_maintenance = now

            consecutive_errors = 0
            time.sleep(max(0.05, float(os.getenv("LOOP_SECONDS", "1"))))

        except KeyboardInterrupt:
            if realistic_simulator is not None:
                try: realistic_simulator.stop()
                except Exception: pass
            if EXECUTION_MODE:
                try: live.cancel_all()
                except Exception: pass
            break
        except Exception as exc:
            consecutive_errors += 1
            p(f"LOOP ERROR | {type(exc).__name__}: {exc}")
            traceback.print_exc()
            if realistic_simulator is not None:
                try: realistic_simulator.stop()
                except Exception: pass
            if EXECUTION_MODE:
                try: risk.halt(f"LOOP_ERROR:{type(exc).__name__}")
                except Exception: pass
                try: live.cancel_all()
                except Exception: pass
                raise RuntimeError("LIVE SAFETY STOP: unexpected live-path exception") from exc
            if consecutive_errors >= 10:
                raise
            time.sleep(2)


if __name__ == "__main__":
    main()
