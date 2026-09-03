import pytest
from clob_adaptive import CLOBAdaptivePlanner


def item(n, price, regime, created=0, token='T', side='Up'):
    return {
        'status':'queued','notional':n,'price':price,'created_at':created,
        'condition':'C','token':token,'side':side,
        'meta':{'regime':regime}
    }


def test_high_single_signal_gets_minimum_lot_without_large_uplift():
    p=CLOBAdaptivePlanner(max_order=5,batch_window_seconds=6)
    plan=p.plan([item(4.35,0.97,'HIGH')], current_ask=0.98, min_shares=5, tick_size=.01, now=1)
    assert plan is not None
    assert plan.execution_price == .98
    assert round(plan.requested_budget,2) == 4.90
    assert plan.topup > 0


def test_mid_signal_batches_before_minimum():
    p=CLOBAdaptivePlanner(max_order=5,batch_window_seconds=6)
    items=[item(.40,.50,'MID',created=i) for i in range(8)]
    plan=p.plan(items, current_ask=.51, min_shares=5, tick_size=.01, now=2)
    assert plan is not None
    assert plan.requested_budget <= 5
    assert plan.requested_budget >= 2.55


def test_price_ceiling_rejects_bad_move():
    p=CLOBAdaptivePlanner()
    plan=p.plan([item(4.35,.90,'HIGH')], current_ask=.94, min_shares=5, tick_size=.01, now=1)
    assert plan is None


def test_small_cheap_signal_can_use_bounded_minimum_topup():
    p=CLOBAdaptivePlanner()
    plan=p.plan([item(.10,.03,'CHEAP')], current_ask=.04, min_shares=5, tick_size=.01, now=1)
    assert plan is not None
    assert round(plan.requested_budget,2) == .20


def test_plan_never_exceeds_single_order_cap():
    p=CLOBAdaptivePlanner(max_order=5)
    items=[item(1.0,.80,'CORE',created=i) for i in range(20)]
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
