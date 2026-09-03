import tempfile
from pathlib import Path
import pytest
from strategy import CapitalFirstStrategy
from paper_ledger import PaperLedger
from research_logger import ResearchLogger

def S(): return CapitalFirstStrategy(min_trade_gap_seconds=0,hard_cutoff_seconds=60,max_total_exposure=300)
def H(p,now=1000): return [{"ts":now-30,"best_bid":p},{"ts":now-10,"best_bid":p},{"ts":now-5,"best_bid":p},{"ts":now,"best_bid":p}]

def test_all_fine_bands():
    s=S(); expected=[(.02,'C00_05','CHEAP'),(.07,'C05_10','CHEAP'),(.12,'C10_15','CHEAP'),(.17,'C15_20','CHEAP'),(.25,'C20_30','CHEAP'),(.35,'M30_40','MID'),(.45,'M40_50','MID'),(.55,'M50_60','MID'),(.65,'M60_70','MID'),(.75,'R70_80','CORE'),(.85,'R80_90','CORE'),(.925,'H90_95','HIGH'),(.975,'H95_100','HIGH')]
    for price,band,regime in expected: assert s.fine_band(price)==(band,regime)

def test_40pct_high_sizing():
    s=S(); assert s.entry_target(.96,'BTC',0)==pytest.approx(13.06008,abs=.01)

def test_high_candidate_not_blocked():
    s=S(); c=s._candidate('BTC','Up',.96,.99,0,H(.96),1000,None,0,0); assert c and c['regime']=='HIGH'

def test_total_300_cap():
    s=S(); x=s.decide(30,.99,.50,.96,.49,H(.96),H(.49),0,1000,now=1000,total_exposure=295,asset='BTC',market='BTC',process_target_band='H95_100'); assert x and 0 < x.notional <= 15.0

def test_no_depth_or_spread_gate():
    s=S(); assert s._candidate('BTC','Up',.96,.99,0,H(.96),1000,None,0,0) is not None

def test_final_minute_cutoff():
    assert S().decide(180,.51,.21,.50,.20,H(.50),H(.20),0,1000,now=1000,asset='BTC',market='BTC',process_target_band='M40_50') is None

def test_side_persistence_preference_path():
    s=S(); x=s.decide(120,.81,.99,.80,.98,H(.80),H(.98),0,1000,now=1000,market_entry_count=1,seconds_since_first_entry=90,thesis_side='Up',asset='BTC',market='BTC',process_target_band='R80_90'); assert x and x.side=='Up'

def test_trajectory_gradient():
    s=S(); falling=[{'ts':970,'best_bid':.25},{'ts':995,'best_bid':.24},{'ts':1000,'best_bid':.20}]; rising=[{'ts':970,'best_bid':.75},{'ts':995,'best_bid':.79},{'ts':1000,'best_bid':.80}]; a=s._candidate('BTC','Up',.20,.21,0,falling,1000,None,0,0); b=s._candidate('BTC','Up',.80,.81,0,rising,1000,None,0,0); assert a['trajectory_likelihood']==.564 and b['trajectory_likelihood']==.542

def test_empirical_data_loaded():
    s=S(); assert len(s.fine_band_trade_share)==13 and len(s.entry_medians)==13 and s.notional_scale==pytest.approx(.4)

def test_cash_constraint():
    s=S(); x=s.decide(30,.99,.50,.96,.49,H(.96),H(.49),0,1.0,now=1000,asset='BTC',market='BTC',process_target_band='H95_100'); assert x and x.notional<=1.0

def test_empirical_process_target_band_changes_candidate_choice():
    s=S(); up=s._candidate('BTC','Up',.96,.97,0,H(.96),1000,None,0,0); down=s._candidate('BTC','Down',.25,.26,0,H(.25),1000,None,0,0); assert s.choose_process_candidate([up,down],'H95_100')['band']=='H95_100'; assert s.choose_process_candidate([up,down],'C20_30')['band']=='C20_30'

def test_empirical_cadence_has_observed_distribution():
    s=S(); vals=[s.sample_delay() for _ in range(5000)]; assert min(vals)==0.0 and max(vals)>=100.0

def test_side_persistence_is_empirical():
    s=S(); vals=[s.process.should_continue_side() for _ in range(50000)]; rate=sum(vals)/len(vals); assert .88 < rate < .906

def test_no_synthetic_band_trajectory_product():
    src=Path(__file__).parents[1].joinpath('strategy.py').read_text(); assert 'band_prior*trajectory_likelihood' not in src and 'band_prior * trajectory_likelihood' not in src

def test_resolution_accounting_and_research():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); ledger=PaperLedger(root/'paper_state.json',1000); m={'id':'m1','condition':'c1','slug':'btc-updown-5m-1000000000','asset':'BTC','market':'BTC Up or Down','start_ts':1000000000.0,'end_ts':1000000300.0}; ledger.buy('c1','up-token',m['market'],'Up',.20,1.0,1000000010,meta={'asset':'BTC','slug':m['slug'],'market_id':'m1'}); closed=ledger.settle('c1','up-token'); assert len(closed)==1 and closed[0]['pnl']==4.0; logger=ResearchLogger(root); logger.record_resolution(ts=1000000302,market=m,winner='Up',winner_token='up-token',closed=closed); assert 'RESOLVED' in (root/'resolutions.csv').read_text()


def test_v152_exact_trader_distribution_targets():
    s = S()
    assert s.VERSION == "V15.2_40PCT_EXACT_DISTRIBUTION"
    assert s.distribution.trade_targets["C00_05"] == pytest.approx(0.11041669879555234)
    assert s.distribution.trade_targets["H95_100"] == pytest.approx(0.04116738378339476)
    assert s.distribution.capital_targets["C00_05"] == pytest.approx(0.011838457873697585)
    assert s.distribution.capital_targets["H95_100"] == pytest.approx(0.452749963176931)


def test_v152_controller_prefers_underrepresented_high():
    s = S()
    for _ in range(25):
        s.observe_trade_distribution("M50_60", 1.0)
    band = s.choose_distribution_band([
        {"band": "H95_100", "target": 13.0},
        {"band": "M50_60", "target": 2.0},
    ])
    assert band == "H95_100"


def test_v152_cadence_api_is_live():
    s = S()
    gap = s.cadence.sample_gap()
    assert gap >= 0.0
