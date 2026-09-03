from pathlib import Path

import pytest

from live_ledger import LiveLedger
from live_risk import LiveRisk


def test_live_ledger_books_only_confirmed_maker_fill_and_deduplicates(tmp_path):
    ledger = LiveLedger(tmp_path / "live.json", 10)
    ledger.record_order("order-1", "cond-1", "token-1", "Up", 0.95, 1.0, "BTC")

    unrelated = {
        "id": "trade-unrelated", "status": "TRADE_STATUS_CONFIRMED", "asset_id": "token-x",
        "price": "0.5", "size": "10", "maker_orders": []
    }
    matched = {
        "id": "trade-1", "status": "MATCHED", "asset_id": "token-1",
        "price": "0.95", "size": "0.7",
        "maker_orders": [{"order_id": "order-1", "matched_amount": "0.7", "price": "0.95", "fee_rate_bps": "0"}],
    }
    confirmed = {**matched, "status": "TRADE_STATUS_CONFIRMED", "transaction_hash": "0xtx", "trader_side": "MAKER"}

    assert ledger.sync_trades([unrelated]) == []
    assert ledger.sync_trades([matched]) == []
    assert ledger.total_open_cost() == pytest.approx(0.0)
    fills = ledger.sync_trades([confirmed])
    assert len(fills) == 1
    assert ledger.cash == pytest.approx(10 - 0.665)
    assert ledger.total_open_cost() == pytest.approx(0.665)
    assert ledger.sync_trades([confirmed]) == []


def test_live_ledger_rejects_overfill_against_order_reservation(tmp_path):
    ledger = LiveLedger(tmp_path / "live.json", 10)
    ledger.record_order("order-1", "cond-1", "token-1", "Up", 0.95, 0.5, "BTC")
    trade = {
        "id": "trade-1", "status": "TRADE_STATUS_CONFIRMED", "asset_id": "token-1",
        "trader_side": "MAKER",
        "maker_orders": [{"order_id": "order-1", "matched_amount": "1.0", "price": "0.95"}],
    }
    with pytest.raises(RuntimeError, match="exceeds remaining order reservation"):
        ledger.sync_trades([trade])


def test_live_ledger_books_intentional_taker_execution(tmp_path):
    ledger = LiveLedger(tmp_path / "live.json", 10)
    ledger.record_order("order-1", "cond-1", "token-1", "Up", 0.95, 0.5, "BTC")
    trade = {
        "id": "trade-1", "status": "TRADE_STATUS_CONFIRMED", "asset_id": "token-1",
        "trader_side": "TAKER", "taker_order_id": "order-1", "size": "0.5", "price": "0.95",
        "fee_rate_bps": "0.07",
    }
    fills = ledger.sync_trades([trade])
    assert len(fills) == 1
    assert ledger.total_open_cost() == pytest.approx(0.475)
    assert ledger.cash < 10


def test_live_ledger_persists_restart_state(tmp_path):
    path = tmp_path / "live.json"
    a = LiveLedger(path, 10)
    a.record_order("order-1", "cond-1", "token-1", "Up", 0.9, 0.5, "BTC")
    a.sync_trades([{
        "id": "trade-1", "status": "TRADE_STATUS_CONFIRMED", "asset_id": "token-1",
        "trader_side": "MAKER",
        "maker_orders": [{"order_id": "order-1", "matched_amount": "0.5", "price": "0.9"}],
    }])
    b = LiveLedger(path, 10)
    assert b.cash == pytest.approx(9.55)
    assert len(b.positions) == 1
    assert any(t.get("trade_id") == "trade-1" for t in b.trades)


def test_live_risk_hard_ceiling_cannot_be_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_BANKROLL_CAP", "999")
    monkeypatch.setenv("MAX_TOTAL_EXPOSURE", "999")
    monkeypatch.setenv("MAX_SINGLE_ORDER", "999")
    monkeypatch.setenv("MAX_MARKET_EXPOSURE", "999")
    monkeypatch.setenv("MAX_OPEN_ORDERS", "999")
    monkeypatch.setenv("MAX_DAILY_LOSS", "999")
    r = LiveRisk(tmp_path / "risk.json")
    assert r.bankroll_cap == 100
    assert r.max_total == 100
    assert r.max_order == 15
    assert r.max_market == pytest.approx(33.3333333333)
    assert r.max_orders == 20
    assert r.daily_loss == 10


def test_live_risk_bankroll_cap_can_only_lower_total_exposure(monkeypatch):
    monkeypatch.setenv("LIVE_BANKROLL_CAP", "2")
    r = LiveRisk()
    assert r.max_total == 2
    assert r.authorize(1, 0, 0, 0, 2)[0]
    assert not r.authorize(1.01, 1, 0, 0, 2)[0]


def test_live_risk_reserved_cash_and_market_exposure(tmp_path):
    r = LiveRisk(tmp_path / "risk.json")
    assert not r.authorize(1, 0, 0, 0, 1, reserved=0.5)[0]
    assert not r.authorize(1, 0, 32.5, 0, 100, reserved=0.5)[0]


def test_live_risk_persists_daily_halt(tmp_path):
    path = tmp_path / "risk.json"
    r = LiveRisk(path)
    r.record_realized(-10)
    assert not r.authorize(1, 0, 0, 0, 100)[0]
    r2 = LiveRisk(path)
    assert not r2.authorize(1, 0, 0, 0, 100)[0]


def test_live_bot_has_no_paper_ledger_in_live_branch():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert "ledger = LiveLedger" in src
    assert 'live.post_only_buy(token, signal.price, notion)' not in src
    assert 'ledger = PaperLedger(DATA / "paper_state.json"' in src


def test_live_order_path_is_post_only_gtc():
    src = Path(__file__).parents[1].joinpath("live_clob.py").read_text()
    assert "create_and_post_order" in src
    assert "post_only=True" in src
    assert "OrderType.GTC" in src


def test_live_mode_forbids_fresh_start():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert "LIVE_TRADING=true" in src
    assert "FRESH_START=true is forbidden" in src


def test_live_shutdown_has_cancel_all():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert "live.cancel_all()" in src


def test_live_daily_loss_halt_cancels_all():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert 'if risk.halted:' in src
    assert 'raise RuntimeError(f"LIVE RISK HALT: {risk.halt_reason}")' in src


def test_live_market_discovery_fails_closed_on_missing_order_flags():
    src = Path(__file__).parents[1].joinpath("market_discovery.py").read_text()
    assert "m.get('acceptingOrders') is True" in src
    assert "m.get('enableOrderBook') is True" in src


def test_live_clob_post_only_path_uses_market_minimum_and_v2_order(monkeypatch):
    import sys
    import types

    class FakeApiCreds:
        def __init__(self, api_key, api_secret, api_passphrase):
            self.api_key = api_key
            self.api_secret = api_secret
            self.api_passphrase = api_passphrase

    class FakeBalanceParams:
        COLLATERAL = "COLLATERAL"
        def __init__(self, asset_type=None):
            self.asset_type = asset_type

    class FakeOrderArgs:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)

    class FakeOptions:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)

    class FakeOrderType: GTC = "GTC"
    class FakeSide: BUY = "BUY"
    class FakeAssetType: COLLATERAL = "COLLATERAL"

    class FakeClient:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def get_clob_market_info(self, condition): return {"mos": 1.0, "mts": "0.01"}
        def get_tick_size(self, token): return "0.01"
        def get_neg_risk(self, token): return False
        def create_and_post_order(self, **kwargs):
            assert kwargs["order_type"] == "GTC"
            assert kwargs["post_only"] is True
            assert kwargs["order_args"].size == pytest.approx(2.0)
            return {"orderID": "o1", "status": "LIVE"}
        def get_ok(self): return "OK"
        def get_address(self): return "0xsigner"
        def get_balance_allowance(self, params): return {"balance": "10", "allowance": "10"}

    fake = types.SimpleNamespace(
        ApiCreds=FakeApiCreds,
        AssetType=FakeAssetType,
        BalanceAllowanceParams=FakeBalanceParams,
        ClobClient=FakeClient,
        OrderArgs=FakeOrderArgs,
        OrderType=FakeOrderType,
        PartialCreateOrderOptions=FakeOptions,
        Side=FakeSide,
    )
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake)
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("SIGNATURE_TYPE", "3")
    monkeypatch.setenv("FUNDER_ADDRESS", "0x" + "2" * 40)
    monkeypatch.setenv("POLY_API_KEY", "k")
    monkeypatch.setenv("POLY_API_SECRET", "s")
    monkeypatch.setenv("POLY_API_PASSPHRASE", "p")

    import live_clob
    c = live_clob.LiveCLOB()
    response = c.post_only_buy("token", 0.5, 1.0, "condition")
    assert response["orderID"] == "o1"


def test_live_clob_exposes_exchange_derived_minimum(monkeypatch):
    import sys
    import types

    class C:
        def __init__(self, **kwargs): pass
        def get_clob_market_info(self, condition): return {"mos": 5.0, "mts": "0.01"}
        def get_tick_size(self, token): return "0.01"
        def get_neg_risk(self, token): return False
    fake = types.SimpleNamespace(
        ApiCreds=lambda **k: k,
        AssetType=types.SimpleNamespace(COLLATERAL="COLLATERAL"),
        BalanceAllowanceParams=lambda **k: k,
        ClobClient=C,
        OrderArgs=object,
        OrderType=types.SimpleNamespace(GTC="GTC"),
        PartialCreateOrderOptions=lambda **k: k,
        Side=types.SimpleNamespace(BUY="BUY"),
    )
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake)
    for k, v in {
        "PRIVATE_KEY":"0x"+"1"*64, "SIGNATURE_TYPE":"3", "FUNDER_ADDRESS":"0x"+"2"*40,
        "POLY_API_KEY":"k", "POLY_API_SECRET":"s", "POLY_API_PASSPHRASE":"p"
    }.items(): monkeypatch.setenv(k, v)
    import live_clob
    c = live_clob.LiveCLOB()
    cost, shares = c.minimum_order("token", "condition", .25)
    assert shares == pytest.approx(5.0)
    assert cost == pytest.approx(1.25)


def test_live_clob_rejects_below_market_minimum(monkeypatch):
    import sys
    import types

    class C:
        def __init__(self, **kwargs): pass
        def get_clob_market_info(self, condition): return {"mos": 3.0, "mts": "0.01"}
        def get_tick_size(self, token): return "0.01"
        def get_neg_risk(self, token): return False
    fake = types.SimpleNamespace(
        ApiCreds=lambda **k: k,
        AssetType=types.SimpleNamespace(COLLATERAL="COLLATERAL"),
        BalanceAllowanceParams=lambda **k: k,
        ClobClient=C,
        OrderArgs=object,
        OrderType=types.SimpleNamespace(GTC="GTC"),
        PartialCreateOrderOptions=lambda **k: k,
        Side=types.SimpleNamespace(BUY="BUY"),
    )
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake)
    for k, v in {
        "PRIVATE_KEY":"0x"+"1"*64, "SIGNATURE_TYPE":"3", "FUNDER_ADDRESS":"0x"+"2"*40,
        "POLY_API_KEY":"k", "POLY_API_SECRET":"s", "POLY_API_PASSPHRASE":"p"
    }.items(): monkeypatch.setenv(k, v)
    import live_clob
    c = live_clob.LiveCLOB()
    with pytest.raises(ValueError, match="below market minimum"):
        c.post_only_buy("token", 0.5, 1.0, "condition")


def test_live_execution_path_never_clips_strategy_signal_to_cash():
    text = (Path(__file__).parents[1] / "bot.py").read_text()
    assert "notion = min(" not in text
    assert "LIVE_SIZE_SCALE" in text


def test_live_execution_reconciles_missing_local_orders_before_releasing_reservations():
    text = (Path(__file__).parents[1] / "bot.py").read_text()
    assert "active_ids = [" in text
    assert "live.reconcile_orders(active_ids)" in text
    assert "unknown_ids = [" in text


def test_live_execution_path_is_clob_adaptive():
    text = (Path(__file__).parents[1] / "bot.py").read_text()
    assert "CLOBAdaptivePlanner" in text
    assert "live.adaptive_buy(" in text
    assert "CLOB_ADAPTIVE_FAK" in text
    assert "max_execution_price" in text


def test_live_env_example_uses_isolated_v152_data_dir():
    text = (Path(__file__).parents[1] / "aws" / "v152-bot.env.example").read_text()
    assert "DATA_DIR=/var/lib/v152-100-bot" in text
    assert "DATA_DIR=/var/lib/v153-bot" not in text


def test_live_size_scale_defaults_to_verified_one_third():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert 'float(os.getenv("LIVE_SIZE_SCALE", "0.3333333333333333"))' in src

def test_live_size_scale_is_capped_at_one():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    block = src[src.index("LIVE_SIZE_SCALE = min("):src.index("strategy = CapitalFirstStrategy", src.index("LIVE_SIZE_SCALE = min("))]
    assert "1.0" in block
    assert "LIVE_SIZE_SCALE cannot exceed live/strategy capital ratio" not in block

def test_live_fee_rate_mismatch_fails_closed(tmp_path):
    ledger = LiveLedger(tmp_path / "fee.json", 10)
    ledger.record_order("o-fee", "c-fee", "t-fee", "Up", .5, .5, "BTC")
    with pytest.raises(RuntimeError, match="non-zero fee_rate_bps"):
        ledger.sync_trades([{
            "id":"tr-fee", "status":"CONFIRMED", "asset_id":"t-fee",
            "trader_side":"MAKER",
            "fee_rate_bps":"10",
            "maker_orders":[{"order_id":"o-fee","matched_amount":"1","price":".5"}]
        }])


def test_live_ledger_books_taker_fill_without_overrunning_reservation(tmp_path):
    ledger = LiveLedger(tmp_path / "taker.json", 10)
    ledger.record_order("o-taker", "c-taker", "t-taker", "Up", .5, .5, "BTC")
    with pytest.raises(RuntimeError, match="exceeds remaining order reservation"):
        ledger.sync_trades([{
            "id":"tr-taker", "status":"CONFIRMED", "asset_id":"t-taker",
            "taker_order_id":"o-taker", "size":"2", "price":".5"
        }])
    assert ledger.cash == pytest.approx(10)


def test_v15_2_live_scale_examples():
    scale = 100.0 / 300.0
    assert 1.00 * scale == pytest.approx(1.0 / 3.0)
    assert 13.06 * scale == pytest.approx(4.3533333333)
    assert 0.40 * scale == pytest.approx(0.1333333333)


def test_live_strategy_uses_300_virtual_capital_frame():
    src = Path(__file__).parents[1].joinpath("bot.py").read_text()
    assert "STRATEGY_BANKROLL = 300.0 if EXECUTION_MODE" in src
    assert "max_total_exposure=(300.0 if EXECUTION_MODE" in src
    assert "strategy_cash = ledger.cash / virtual_scale" in src
    assert "strategy_total_exposure = (" in src
    assert "observed_notional = (" in src


def test_execution_queue_never_pools_different_prices(tmp_path):
    from live_execution import LiveExecutionQueue

    q = LiveExecutionQueue(tmp_path / "q.json")
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.54, notional=0.40)
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.51, notional=0.40)
    groups = q.executable_groups("c1", token="t1", side="Up")
    assert sorted(round(g["price"], 2) for g in groups) == [0.51, 0.54]
    assert all(g["notional"] == pytest.approx(0.40) for g in groups)


def test_execution_queue_can_filter_to_exact_price(tmp_path):
    from live_execution import LiveExecutionQueue

    q = LiveExecutionQueue(tmp_path / "q.json")
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.30, notional=0.40)
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.31, notional=0.25)
    groups = q.executable_groups("c1", 0.31, token="t1", side="Up")
    assert len(groups) == 1
    assert groups[0]["price"] == pytest.approx(0.31)
    assert groups[0]["notional"] == pytest.approx(0.25)


def test_execution_queue_pooling_keeps_token_and_side_separate(tmp_path):
    from live_execution import LiveExecutionQueue

    q = LiveExecutionQueue(tmp_path / "q.json")
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.60, notional=0.40)
    q.enqueue(condition="c1", token="t1", side="Down", market="BTC", price=0.60, notional=0.40)
    q.enqueue(condition="c1", token="t2", side="Up", market="BTC", price=0.60, notional=0.40)

    groups = q.executable_groups("c1")
    assert len(groups) == 3
    assert {(g["token"], g["side"]) for g in groups} == {("t1", "Up"), ("t1", "Down"), ("t2", "Up")}


def test_execution_queue_aggregates_same_price_allocations(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "q.json")
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.80, notional=0.03)
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.80, notional=0.04)
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.80, notional=0.05)
    groups = q.executable_groups("c1")
    assert len(groups) == 1
    assert abs(groups[0]["notional"] - 0.12) < 1e-9
    assert len(groups[0]["items"]) == 3


def test_execution_queue_does_not_mix_price_or_side(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "q.json")
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.80, notional=0.03)
    q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.81, notional=0.04)
    q.enqueue(condition="c1", token="t1", side="Down", market="BTC", price=0.80, notional=0.05)
    groups = q.executable_groups("c1")
    assert sorted(round(g["notional"], 6) for g in groups) == [0.03, 0.04, 0.05]


def test_execution_queue_selects_only_complete_allocations(tmp_path):
    from live_execution import LiveExecutionQueue

    items = [{"notional": 2.60}, {"notional": 1.80}, {"notional": 0.50}, {"notional": 0.40}]
    chosen, total = LiveExecutionQueue.select_complete_items(items, 4.80, 5.00)
    assert [x["notional"] for x in chosen] == [2.60, 1.80, 0.50]
    assert total == pytest.approx(4.90)


def test_execution_queue_does_not_partialize_when_no_valid_whole_subset(tmp_path):
    from live_execution import LiveExecutionQueue

    items = [{"notional": 4.35}, {"notional": 1.00}, {"notional": 0.20}]
    chosen, total = LiveExecutionQueue.select_complete_items(items, 4.90, 5.00)
    assert chosen == []
    assert total == pytest.approx(0.0)


def test_execution_queue_deduplicates_unchanged_signal(tmp_path):
    from live_execution import LiveExecutionQueue

    q = LiveExecutionQueue(tmp_path / "q.json")
    meta = {"signal_key": "c|t|Up|0.50|0|0|1.0000000000"}
    first = q.enqueue(condition="c", token="t", side="Up", market="BTC", price=0.50, notional=0.33, meta=meta)
    assert first
    assert q.has_pending_signal(
        condition="c", token="t", side="Up", price=0.50, signal_key=meta["signal_key"]
    )
    q.mark_submitted(q.pending()[0], 0.33, "o1")
    assert not q.has_pending_signal(
        condition="c", token="t", side="Up", price=0.50, signal_key=meta["signal_key"]
    )


def test_executable_groups_support_exact_price_filter(tmp_path):
    from live_execution import LiveExecutionQueue

    q = LiveExecutionQueue(tmp_path / "q.json")
    q.enqueue(condition="c", token="t", side="Up", market="BTC", price=0.15, notional=0.15)
    groups = q.executable_groups("c", 0.20, token="t", side="Up")
    assert groups == []


def test_live_submission_does_not_advance_after_queue_only(tmp_path):
    # Regression test is covered structurally by bot.py:
    # live_order_submitted is required before the live accepted-trade block.
    text = (Path(__file__).parents[1] / "bot.py").read_text()
    assert 'if EXECUTION_MODE and not live_order_submitted:' in text
    assert 'live_order_submitted = True' in text


def test_execution_queue_partial_group_consumption(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "q.json")
    a = q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.80, notional=0.03)
    b = q.enqueue(condition="c1", token="t1", side="Up", market="BTC", price=0.80, notional=0.04)
    group = q.pending_groups("c1")[0]
    q.mark_submitted_group(group, 0.05, "order-1")
    pending = q.pending()
    assert len(pending) == 1
    assert abs(float(pending[0]["notional"]) - 0.02) < 1e-9
    assert pending[0]["id"] == b
