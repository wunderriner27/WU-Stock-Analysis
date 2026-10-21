"""Auto mode: gating, paper-only, placing/blocking, self-stop. Claude and the broker are faked."""
from datetime import datetime

import pytest

from app import ai_autopilot, ai_trader
from test_ai_trader import env, fake_claude, order  # noqa: F401 - env is a fixture


@pytest.fixture
def auto(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AI_TRADER_AUTO", "yes")
    monkeypatch.setattr(ai_autopilot, "market_open", lambda now=None: False)  # keep the loop idle
    ai_autopilot._state.update(enabled=False, errors=0, stopped_reason=None, running=False, next_run=None)
    ai_autopilot._activity.clear()
    yield env
    if ai_autopilot._state["enabled"]:
        env.client.post("/api/ai-trader/auto", json={"enabled": False})


def turn_on(env, **kw):
    body = {"enabled": True, "instruction": "buy dips", "symbols": ["AAPL"], "interval_min": 60, **kw}
    return env.client.post("/api/ai-trader/auto", json=body)


def test_needs_env_opt_in(auto):
    auto.mp.setenv("AI_TRADER_AUTO", "no")
    r = turn_on(auto)
    assert r.status_code == 403 and "AI_TRADER_AUTO" in r.json()["detail"]


def test_refuses_live_account(auto):
    auto.trader.paper = False
    assert turn_on(auto).status_code == 403


def test_run_places_valid_and_blocks_rest(auto):
    auto.mp.setattr(ai_trader, "_claude", lambda: fake_claude([order(qty=2), order(qty=50)]))
    assert turn_on(auto).json()["enabled"] is True
    r = auto.client.post("/api/ai-trader/auto/run")
    assert r.status_code == 200
    [entry] = r.json()["activity"]
    assert [p["order"] for p in entry["placed"]] == ["BUY 2 AAPL"]
    assert entry["blocked"][0]["order"] == "BUY 50 AAPL" and "per-order limit" in entry["blocked"][0]["reason"]
    assert auto.trader.submitted == [("AAPL", 2, "buy", "market")]
    assert ai_trader._proposals == {}  # nothing left lying around to execute later


def test_live_mid_session_stops(auto):
    auto.mp.setattr(ai_trader, "_claude", lambda: fake_claude([order(qty=1)]))
    turn_on(auto)
    auto.trader.paper = False
    r = auto.client.post("/api/ai-trader/auto/run")
    assert r.status_code == 409
    s = auto.client.get("/api/ai-trader/auto").json()
    assert s["enabled"] is False and "paper" in s["stopped_reason"] and auto.trader.submitted == []


def test_stops_after_repeated_failures(auto):
    async def boom(**kwargs):
        raise RuntimeError("api down")
    auto.mp.setattr(ai_trader, "_claude", lambda: type("C", (), {"beta": type("B", (), {
        "messages": type("M", (), {"parse": staticmethod(boom)})})})())
    turn_on(auto)
    for _ in range(ai_autopilot.MAX_ERRORS):
        auto.client.post("/api/ai-trader/auto/run")
    s = auto.client.get("/api/ai-trader/auto").json()
    assert s["enabled"] is False and "in a row failed" in s["stopped_reason"]


def test_market_hours():
    tz = ai_trader.MARKET_TZ
    assert ai_autopilot.market_open(datetime(2026, 10, 21, 10, 0, tzinfo=tz))
    assert not ai_autopilot.market_open(datetime(2026, 10, 21, 16, 0, tzinfo=tz))
    assert not ai_autopilot.market_open(datetime(2026, 10, 24, 11, 0, tzinfo=tz))  # Saturday
