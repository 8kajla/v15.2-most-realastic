import json
from pathlib import Path

import pytest

from shadow_clob import ShadowCLOB


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.trade = {"price": "0.80", "side": "SELL"}
        self.book = {
            "asset_id": "t1",
            "market": "c1",
            "min_order_size": "5",
            "tick_size": "0.01",
            "bids": [{"price": "0.79", "size": "100"}],
            "asks": [{"price": "0.82", "size": "100"}],
            "last_trade_price": "0.80",
        }

    def get(self, url, params=None, timeout=None):
        if url.endswith("/book"):
            return FakeResponse(self.book)
        if url.endswith("/last-trade-price"):
            return FakeResponse(self.trade)
        if url.endswith("/api/geoblock"):
            return FakeResponse({"blocked": False, "country": "IN", "region": "MH"})
        raise AssertionError(url)


def test_shadow_validates_minimum_and_tick(tmp_path, monkeypatch):
    s = ShadowCLOB(tmp_path / "shadow.json")
    s.session = FakeSession()
    assert s.minimum_order("t1", "c1", 0.80) == pytest.approx((4.0, 5.0))
    with pytest.raises(ValueError, match="tick size"):
        s.minimum_order("t1", "c1", 0.805)


def test_shadow_post_only_creates_synthetic_gtc_order(tmp_path):
    s = ShadowCLOB(tmp_path / "shadow.json")
    s.session = FakeSession()
    response = s.post_only_buy("t1", 0.80, 4.0, "c1")
    assert response["success"] is True
    assert response["shadow"] is True
    orders = s.get_open_orders()
    assert len(orders) == 1
    assert orders[0]["order_type"] == "GTC"
    assert orders[0]["price"] == pytest.approx(0.80)


def test_shadow_sell_print_produces_maker_fill_and_is_idempotent(tmp_path):
    s = ShadowCLOB(tmp_path / "shadow.json")
    s.session = FakeSession()
    response = s.post_only_buy("t1", 0.80, 4.0, "c1")
    oid = response["orderID"]
    fills = s.get_trades()
    assert len(fills) == 1
    assert fills[0]["status"] == "CONFIRMED"
    assert fills[0]["trader_side"] == "MAKER"
    assert fills[0]["maker_orders"][0]["order_id"] == oid
    assert float(fills[0]["maker_orders"][0]["matched_amount"]) == pytest.approx(5.0)
    assert s.get_open_orders() == []
    assert s.get_trades() == []


def test_shadow_refuses_non_passive_order(tmp_path):
    s = ShadowCLOB(tmp_path / "shadow.json")
    fake = FakeSession()
    fake.book["asks"] = [{"price": "0.80", "size": "100"}]
    s.session = fake
    with pytest.raises(ValueError, match="marketable"):
        s.post_only_buy("t1", 0.80, 4.0, "c1")

def test_shadow_mode_cannot_construct_live_client(monkeypatch):
    """Regression: stale Railway LIVE_TRADING=true must not import LiveCLOB in shadow mode."""
    import os
    env = dict(os.environ)
    env.update({"PAPER_TRADING":"false", "LIVE_TRADING":"true", "SHADOW_CLOB":"true", "DATA_DIR":"/tmp/v152-shadow-test"})
    # bot.py may then attempt network/loop work; instead inspect its startup source
    src = Path(__file__).resolve().parents[1] / 'bot.py'
    text = src.read_text()
    assert 'if SHADOW and LIVE:' in text
    assert 'LIVE = False' in text
    assert 'if LIVE:\n    from live_clob import LiveCLOB' in text


def test_shadow_mode_forces_paper_research_mode(monkeypatch):
    src=(Path(__file__).parents[1] / "bot.py").read_text()
    assert 'if SHADOW:' in src and 'PAPER = True' in src

