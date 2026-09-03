import pytest
from clob_adaptive import CLOBAdaptivePlanner


def item(n, price, regime, created=0, token='T', side='Up', fine_band=None):
    if fine_band is None:
        if price < 0.05: fine_band = 'C00_05'
        elif price < 0.10: fine_band = 'C05_10'
        elif price < 0.15: fine_band = 'C10_15'
        elif price < 0.20: fine_band = 'C15_20'
        elif price < 0.30: fine_band = 'C20_30'
        elif price < 0.40: fine_band = 'M30_40'
        elif price < 0.50: fine_band = 'M40_50'
        elif price < 0.60: fine_band = 'M50_60'
        elif price < 0.70: fine_band = 'M60_70'
        elif price < 0.80: fine_band = 'R70_80'
        elif price < 0.90: fine_band = 'R80_90'
        elif price < 0.95: fine_band = 'H90_95'
        else: fine_band = 'H95_100'
    return {
        'status':'queued','notional':n,'price':price,'created_at':created,
        'condition':'C','token':token,'side':side,
        'meta':{'regime':regime,'fine_band':fine_band}
    }


def test_current_ask_can_move_within_same_strategy_band_without_topup():
    p=CLOBAdaptivePlanner(max_order=5,batch_window_seconds=6)
    plan=p.plan([item(4.70,0.92,'HIGH')], current_ask=0.94, min_shares=5, tick_size=.01, now=1)
    assert plan is not None
    assert plan.execution_price == .94
    assert plan.requested_budget == pytest.approx(4.70)
    assert plan.topup == pytest.approx(0.0)


def test_current_ask_outside_signal_band_is_rejected():
    p=CLOBAdaptivePlanner(max_order=5,batch_window_seconds=6)
    plan=p.plan([item(2.50,0.92,'HIGH')], current_ask=0.95, min_shares=5, tick_size=.01, now=1)
    assert plan is None


def test_mid_signal_batches_same_band_before_exchange_minimum():
    p=CLOBAdaptivePlanner(max_order=5,batch_window_seconds=6)
    items=[item(.40,.50,'MID',created=i, fine_band='M50_60') for i in range(8)]
    plan=p.plan(items, current_ask=.51, min_shares=5, tick_size=.01, now=2)
    assert plan is not None
    assert plan.requested_budget == pytest.approx(2.8)
    assert len(plan.items) == 7
    assert plan.topup == pytest.approx(0.0)


def test_small_signals_wait_for_real_compatible_capital_not_synthetic_topup():
    p=CLOBAdaptivePlanner()
    one=item(.10,.03,'CHEAP',fine_band='C00_05')
    assert p.plan([one], current_ask=.04, min_shares=5, tick_size=.01, now=1) is None
    two=item(.10,.04,'CHEAP',created=1,fine_band='C00_05')
    plan=p.plan([one,two], current_ask=.04, min_shares=5, tick_size=.01, now=1)
    assert plan is not None
    assert plan.requested_budget == pytest.approx(.20)
    assert plan.topup == pytest.approx(0.0)


def test_never_batches_different_fine_bands():
    p=CLOBAdaptivePlanner(max_order=5,batch_window_seconds=6)
    items=[item(.40,.50,'MID',created=i,fine_band='M50_60') for i in range(7)] + [item(.40,.60,'MID',created=1,fine_band='M60_70')]
    plan=p.plan(items, current_ask=.51, min_shares=5, tick_size=.01, now=2)
    assert plan is not None
    assert len(plan.items) == 7
    assert all(x['meta']['fine_band'] == 'M50_60' for x in plan.items)


def test_none_current_ask_is_safe_wait():
    p=CLOBAdaptivePlanner()
    assert p.plan([item(1.0,.50,'MID')], current_ask=None, min_shares=5, tick_size=.01, now=1) is None

def test_invalid_current_ask_is_safe_wait():
    p=CLOBAdaptivePlanner()
    assert p.plan([item(1.0,.50,'MID')], current_ask='not-a-price', min_shares=5, tick_size=.01, now=1) is None

def test_plan_never_exceeds_single_order_cap():
    p=CLOBAdaptivePlanner(max_order=5)
    items=[item(1.0,.80,'CORE',created=i,fine_band='R80_90') for i in range(20)]
    plan=p.plan(items, current_ask=.81, min_shares=5, tick_size=.01, now=5)
    assert plan is not None
    assert plan.requested_budget <= 5.0000001
    assert plan.order_shares * plan.execution_price <= 5.0000001

def test_live_clob_adaptive_buy_uses_fak_and_not_post_only(monkeypatch):
    import sys, types
    calls=[]
    class FakeApiCreds:
        def __init__(self, **k): self.__dict__.update(k)
    class FakeBalanceParams:
        def __init__(self, **k): self.__dict__.update(k)
    class FakeOrderArgs:
        def __init__(self, **k): self.__dict__.update(k)
    class FakeOptions:
        def __init__(self, **k): self.__dict__.update(k)
    class FakeType:
        GTC='GTC'; FAK='FAK'
    class FakeSide: BUY='BUY'
    class FakeAsset: COLLATERAL='COLLATERAL'
    class C:
        def __init__(self, **k): pass
        def get_clob_market_info(self, c): return {'mos':5.0,'mts':'0.01'}
        def get_tick_size(self, t): return '0.01'
        def get_neg_risk(self,t): return False
        def create_and_post_order(self, **kw):
            calls.append(kw); return {'orderID':'fak1','status':'matched'}
    fake=types.SimpleNamespace(ApiCreds=FakeApiCreds, AssetType=FakeAsset, BalanceAllowanceParams=FakeBalanceParams,
        ClobClient=C, OrderArgs=FakeOrderArgs, OrderType=FakeType, PartialCreateOrderOptions=FakeOptions, Side=FakeSide)
    monkeypatch.setitem(sys.modules,'py_clob_client_v2',fake)
    for k,v in {'PRIVATE_KEY':'0x'+'1'*64,'SIGNATURE_TYPE':'3','FUNDER_ADDRESS':'0x'+'2'*40,'POLY_API_KEY':'k','POLY_API_SECRET':'s','POLY_API_PASSPHRASE':'p'}.items(): monkeypatch.setenv(k,v)
    import live_clob
    c=live_clob.LiveCLOB()
    r=c.adaptive_buy('token',.51,5,'condition')
    assert r['orderID']=='fak1'
    assert calls[0]['order_type']=='FAK'
    assert calls[0]['post_only'] is False
    assert calls[0]['order_args'].size == pytest.approx(5)
