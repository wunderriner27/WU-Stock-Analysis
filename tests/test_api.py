import asyncio

import pytest

import main
from app.errors import ProviderTimeout, RateLimited


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["provider"] == "fake"


def test_index_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text


def test_quotes(client):
    r = client.get("/api/quotes", params={"symbols": "aapl, msft"})
    assert r.status_code == 200
    q = r.json()["quotes"]
    assert set(q) == {"AAPL", "MSFT"}
    assert q["AAPL"]["spread"] == pytest.approx(0.1)


@pytest.mark.parametrize("symbols", ["", " , ", "AAPL,DROP TABLE"])
def test_quotes_rejects_bad_symbols(client, symbols):
    assert client.get("/api/quotes", params={"symbols": symbols}).status_code == 400


def test_stock_payload(client):
    r = client.get("/api/stock/aapl")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "AAPL"
    n = len(body["history"]["dates"])
    assert 240 <= n <= 262  # one calendar year of business days
    assert all(len(body["history"][c]) == n for c in ("open", "high", "low", "close", "volume"))
    series = body["indicators"]["series"]
    assert all(len(v) == n for v in series.values())
    # 600 bars of lookback means the 200-day SMA is populated across the whole window
    assert None not in series["sma200"]
    assert body["indicators"]["signals"]
    assert body["performance"]["1Y"] is not None
    assert body["fundamentals"]["sector"] == "Technology"
    assert {n["sentiment"]["label"] for n in body["news"]} == {"bullish", "bearish"}
    assert body["warnings"] == []


def test_stock_unknown_ticker_is_404(client):
    r = client.get("/api/stock/ZZZZ")
    assert r.status_code == 404
    assert r.json()["error"] == "ticker_not_found"


def test_stock_invalid_format_is_400(client):
    assert client.get("/api/stock/AAPL$").status_code == 400


def test_stock_rate_limit_sets_retry_after(client, fake):
    fake.fail["get_history"] = RateLimited("slow down", retry_after=12)
    r = client.get("/api/stock/AAPL")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "12"


def test_stock_degrades_when_optional_sections_fail(client, fake):
    fake.fail["get_quote"] = ProviderTimeout("slow")
    fake.fail["get_fundamentals"] = RuntimeError("boom")
    fake.fail["get_news"] = RuntimeError("boom")
    r = client.get("/api/stock/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body["quote"]["price"] == pytest.approx(body["history"]["close"][-1], abs=1e-4)  # synthesized from last bar
    assert body["fundamentals"] is None and body["news"] == []
    assert len(body["warnings"]) == 3


def test_news(client):
    r = client.get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert [t["symbol"] for t in body["tape"]] == ["SPY", "QQQ", "DIA", "IWM", "^VIX"]
    assert {i["label"] for i in body["indices"]} == {"S&P 500"}
    assert body["news_sentiment"] == {"bullish": 1, "bearish": 1, "neutral": 0}
    assert body["movers"]["gainers"][0]["symbol"] == "AAPL"
    assert isinstance(body["calendar"], list)
    assert body["warnings"] == []


def test_news_degrades_when_sections_fail(client, fake):
    fake.fail["get_quotes"] = RuntimeError("down")
    fake.fail["get_movers"] = RuntimeError("down")
    body = client.get("/api/news").json()
    assert body["tape"] == [] and body["movers"] == {"gainers": [], "losers": []}
    assert len(body["warnings"]) == 2


def test_quotes_for_unknown_symbols_is_empty_not_error(client):
    r = client.get("/api/quotes", params={"symbols": "ZZZZ"})
    assert r.status_code == 200
    assert r.json()["quotes"] == {}


def test_news_warns_when_quotes_come_back_empty(client, fake, monkeypatch):
    async def empty(symbols):
        return {}

    monkeypatch.setattr(fake, "get_quotes", empty)
    body = client.get("/api/news").json()
    assert body["warnings"] == ["Index quotes unavailable: no data returned"]


def test_news_slow_section_hits_deadline(client, fake, monkeypatch):
    monkeypatch.setattr(main, "SOFT_DEADLINE", 0.05)

    async def hang(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(fake, "get_movers", hang)
    body = client.get("/api/news").json()
    assert body["movers"] == {"gainers": [], "losers": []}
    assert body["warnings"] == ["Movers unavailable: timed out"]


# ---------------------------------------------------------------- trading
def test_trading_disabled_without_alpaca(client):
    assert client.get("/api/trading/status").json()["enabled"] is False
    assert client.get("/api/account").status_code == 501
    assert client.post("/api/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"}).status_code == 501


def test_trading_status_and_account(trade_client):
    assert trade_client.get("/api/trading/status").json() == {
        "enabled": True, "mode": "paper", "live_orders_allowed": True}
    body = trade_client.get("/api/account").json()
    assert body["account"]["equity"] == 100_000.0
    assert body["positions"][0]["symbol"] == "AAPL"


def test_place_market_order(trade_client, trader):
    r = trade_client.post("/api/orders", json={"symbol": "aapl", "qty": 0.5, "side": "buy"})
    assert r.status_code == 200
    assert trader.submitted == [("AAPL", 0.5, "buy", "market", "day", None)]


@pytest.mark.parametrize("order, status", [
    ({"symbol": "AAPL", "qty": 1, "side": "buy", "type": "limit"}, 400),          # no limit price
    ({"symbol": "AAPL", "qty": 1.5, "side": "buy", "time_in_force": "gtc"}, 400),  # fractional gtc
    ({"symbol": "AAPL", "qty": 1, "side": "buy", "time_in_force": "ioc"}, 400),
    ({"symbol": "AAPL", "qty": 0, "side": "buy"}, 422),
    ({"symbol": "AAPL", "qty": 1, "side": "short"}, 422),
    ({"symbol": "AA PL", "qty": 1, "side": "buy"}, 400),
])
def test_order_validation(trade_client, trader, order, status):
    assert trade_client.post("/api/orders", json=order).status_code == status
    assert trader.submitted == []


def test_cancel_order_validates_id(trade_client):
    assert trade_client.delete("/api/orders/not-a-uuid").status_code == 400
    oid = "00000000-0000-0000-0000-000000000001"
    assert trade_client.delete(f"/api/orders/{oid}").json() == {"cancelled": oid}


def test_close_position(trade_client):
    assert trade_client.delete("/api/positions/aapl").json()["symbol"] == "AAPL"
