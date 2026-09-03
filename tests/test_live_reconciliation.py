import pytest
from live_ledger import LiveLedger


def test_pending_matched_trade_survives_restart_until_confirmation(tmp_path):
    path = tmp_path / 'state.json'
    a = LiveLedger(path, 10)
    a.record_order('o1', 'c1', 't1', 'Up', .5, 1, 'BTC')
    matched = {
        'id': 'tr1', 'status': 'MATCHED', 'asset_id': 't1', 'price': '.5', 'size': '2',
        'maker_orders': [{'order_id': 'o1', 'matched_amount': '2', 'price': '.5'}]
    }
    assert a.sync_trades([matched]) == []
    b = LiveLedger(path, 10)
    assert 'tr1' in b.pending_trades
    assert b.cash == 10

    confirmed = {**matched, 'status': 'TRADE_STATUS_CONFIRMED', 'trader_side': 'MAKER'}
    assert len(b.sync_trades([confirmed])) == 1
    assert b.cash == pytest.approx(9)


def test_failed_pending_trade_does_not_change_cash(tmp_path):
    a = LiveLedger(tmp_path / 'state.json', 10)
    a.record_order('o1', 'c1', 't1', 'Up', .5, 1, 'BTC')
    matched = {'id':'tr1','status':'MATCHED','asset_id':'t1','maker_orders':[{'order_id':'o1','matched_amount':'2','price':'.5'}]}
    failed = {**matched, 'status':'TRADE_STATUS_FAILED'}
    assert a.sync_trades([matched]) == []
    assert a.sync_trades([failed]) == []
    assert a.cash == pytest.approx(10)
    assert not a.positions
