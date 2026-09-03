import pytest
from live_risk import LiveRisk

def test_live_hard_cap(monkeypatch):
    monkeypatch.setenv('LIVE_BANKROLL_CAP','100'); r=LiveRisk()
    assert r.authorize(1,0,0,0,100)[0]
    assert not r.authorize(15.01,0,0,0,100)[0]
def test_total_cap(): assert not LiveRisk().authorize(1,99.1,0,0,100)[0]
def test_market_cap(): assert not LiveRisk().authorize(5,0,29,0,100)[0]
def test_open_order_cap(): assert not LiveRisk().authorize(1,0,0,20,100)[0]
def test_daily_loss_cap():
    r=LiveRisk(); r.record_realized(-10); assert not r.authorize(1,0,0,0,100)[0]


def test_execution_queue_preserves_small_signal(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "queue.json")
    qid = q.enqueue(condition="c", token="t", side="Up", market="BTC", price=.25, notional=.03)
    assert qid
    assert q.pending()[0]["notional"] == pytest.approx(.03)
    q.mark_submitted(q.pending()[0], .02, "o1")
    assert q.pending()[0]["notional"] == pytest.approx(.01)


def test_execution_queue_minimum_cost():
    from live_execution import LiveExecutionQueue
    assert LiveExecutionQueue.min_order_cost(.25, 5) == pytest.approx(1.25)


def test_execution_queue_expires_market(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "queue.json")
    q.enqueue(condition="c", token="t", side="Up", market="BTC", price=.5, notional=.03)
    assert q.expire_condition("c", "MARKET_CUTOFF") == pytest.approx(.03)
    assert not q.pending()


def test_execution_queue_preserves_small_signal(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "queue.json")
    qid = q.enqueue(condition="c", token="t", side="Up", market="BTC", price=.25, notional=.03)
    assert qid
    assert q.pending()[0]["notional"] == pytest.approx(.03)
    q.mark_submitted(q.pending()[0], .02, "o1")
    assert q.pending()[0]["notional"] == pytest.approx(.01)


def test_execution_queue_minimum_cost():
    from live_execution import LiveExecutionQueue
    assert LiveExecutionQueue.min_order_cost(.25, 5) == pytest.approx(1.25)


def test_execution_queue_expires_market(tmp_path):
    from live_execution import LiveExecutionQueue
    q = LiveExecutionQueue(tmp_path / "queue.json")
    q.enqueue(condition="c", token="t", side="Up", market="BTC", price=.5, notional=.03)
    assert q.expire_condition("c", "MARKET_CUTOFF") == pytest.approx(.03)
    assert not q.pending()
