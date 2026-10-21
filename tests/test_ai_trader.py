"""AI trader: limits, propose -> execute flow. Claude and the broker are faked; no network."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import main
from app import ai_trader
from app.providers.base import Quote


class FakeProvider:
    name = "fake"

    async def get_history(self, symbol, period_days=365):
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=260)
        close = pd.Series(np.linspace(80, 100, len(idx)), index=idx)
        return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                             "volume": 1e6}, index=idx)

    async def get_quote(self, symbol):
        return Quote(symbol=symbol, price=100.0, prev_close=99.0, change=1.0, change_pct=1.01, bid=99.9, ask=100.1)

    async def get_news(self, symbol=None, limit=20):
        return []


class FakeTrader:
    def __init__(self, paper=True, live_orders_allowed=True):
        self.paper, self.live_orders_allowed = paper, live_orders_allowed
        self.submitted = []

    async def get_account(self):
        return {"equity": 100_000.0, "cash": 50_000.0, "buying_power": 50_000.0,
                "mode": "paper" if self.paper else "LIVE", "live_orders_allowed": self.live_orders_allowed,
                "trading_blocked": False}

    async def get_positions(self):
        return [{"symbol": "MSFT", "qty": 5.0, "market_value": 500.0}]

    async def submit_order(self, symbol, qty, side, type="market", time_in_force="day", limit_price=None):
        self.submitted.append((symbol, qty, side, type))
        return {"id": "o1", "symbol": symbol, "qty": qty, "side": side, "status": "accepted"}


def fake_claude(orders):
    plan = ai_trader.TradePlan(summary="ok", risks=["r"], orders=[ai_trader.ProposedOrder(**o) for o in orders])

    async def parse(**kwargs):
        assert kwargs["fallbacks"] == "default"
        return SimpleNamespace(stop_reason="end_turn", parsed_output=plan)

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))


def order(**kw):
    base = dict(symbol="AAPL", side="buy", qty=5, type="market", limit_price=None,
                time_in_force="day", rationale="because", confidence="medium")
    return {**base, **kw}


@pytest.fixture
def env(monkeypatch):
    trader = FakeTrader()
    monkeypatch.setattr(ai_trader, "get_trading_provider", lambda: trader)
    monkeypatch.setattr(ai_trader, "get_provider", lambda: FakeProvider())
    ai_trader._proposals.clear()
    ai_trader._orders_today.clear()
    for k in ("AI_TRADER_MAX_ORDER_USD", "AI_TRADER_MAX_POSITION_PCT", "AI_TRADER_MAX_ORDERS_PER_DAY",
              "AI_TRADER_ALLOW_SHORTS", "AI_TRADER_ALLOW_LIVE"):
        monkeypatch.delenv(k, raising=False)
    return SimpleNamespace(trader=trader, client=TestClient(main.app), mp=monkeypatch)


def propose(env, orders):
    env.mp.setattr(ai_trader, "_claude", lambda: fake_claude(orders))
    r = env.client.post("/api/ai-trader/propose", json={"instruction": "trade for me", "symbol": "AAPL"})
    assert r.status_code == 200, r.text
    return r.json()["orders"]


def test_valid_order_executes_once(env):
    [o] = propose(env, [order()])
    assert o["violations"] == [] and o["est_value"] == pytest.approx(500.5)
    r = env.client.post(f"/api/ai-trader/proposals/{o['id']}/execute")
    assert r.status_code == 200 and env.trader.submitted == [("AAPL", 5, "buy", "market")]
    assert env.client.post(f"/api/ai-trader/proposals/{o['id']}/execute").status_code == 404


def test_limits_block_orders(env):
    big, short, pos = propose(env, [order(qty=20), order(symbol="MSFT", side="sell", qty=10),
                                    order(qty=9)])
    assert any("per-order limit" in v for v in big["violations"])
    assert any("short selling is off" in v for v in short["violations"])
    env.mp.setenv("AI_TRADER_MAX_POSITION_PCT", "0.5")  # $500 cap on $100k equity
    r = env.client.post(f"/api/ai-trader/proposals/{pos['id']}/execute")
    assert r.status_code == 409 and "equity cap" in r.json()["detail"]
    assert env.trader.submitted == []


def test_daily_limit(env):
    env.mp.setenv("AI_TRADER_MAX_ORDERS_PER_DAY", "1")
    a, b = propose(env, [order(qty=1), order(qty=1)])
    assert env.client.post(f"/api/ai-trader/proposals/{a['id']}/execute").status_code == 200
    r = env.client.post(f"/api/ai-trader/proposals/{b['id']}/execute")
    assert r.status_code == 409 and "Daily AI order limit" in r.json()["detail"]


def test_live_account_needs_opt_in(env):
    env.trader.paper = False
    [o] = propose(env, [order(qty=1)])
    assert any("Live account" in v for v in o["violations"])
    env.mp.setenv("AI_TRADER_ALLOW_LIVE", "yes")
    assert env.client.post(f"/api/ai-trader/proposals/{o['id']}/execute").status_code == 200


def test_disabled_without_trading(env):
    env.mp.setattr(ai_trader, "get_trading_provider", lambda: None)
    assert env.client.get("/api/ai-trader/status").json()["enabled"] is False
    assert env.client.post("/api/ai-trader/propose", json={"instruction": "x"}).status_code == 501
