"""Shared fixtures. Every test runs offline: routes are served by FakeProvider,
never by Yahoo or Alpaca."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import main
from app.errors import TickerNotFound
from app.providers.base import Fundamentals, MarketDataProvider, NewsItem, Quote, TradingProvider
from app.resilience import cache


def make_ohlcv(days: int = 600, start: float = 100.0, seed: int = 7) -> pd.DataFrame:
    """Deterministic random-walk daily bars on business days."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp("2026-10-20"), periods=days)
    close = start * np.exp(np.cumsum(rng.normal(0.0005, 0.015, days)))
    open_ = close * (1 + rng.normal(0, 0.003, days))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, days))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, days))
    vol = rng.integers(1_000_000, 5_000_000, days).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


class FakeProvider(MarketDataProvider):
    name = "fake"

    def __init__(self):
        self.known = {"AAPL", "MSFT", "SPY", "QQQ", "DIA", "IWM", "^VIX", "^GSPC", "^TNX", "GC=F"}
        self.fail: dict[str, Exception] = {}  # method name -> exception to raise

    def _maybe_fail(self, method: str):
        if method in self.fail:
            raise self.fail[method]

    def _quote(self, s: str) -> Quote:
        return Quote(symbol=s, price=101.0, prev_close=100.0, change=1.0, change_pct=1.0,
                     bid=100.95, ask=101.05, volume=1e6)

    async def get_quote(self, symbol):
        self._maybe_fail("get_quote")
        if symbol not in self.known:
            raise TickerNotFound(f"No quote available for '{symbol}'.", ticker=symbol)
        return self._quote(symbol)

    async def get_quotes(self, symbols):
        self._maybe_fail("get_quotes")
        return {s: self._quote(s) for s in symbols if s in self.known}

    async def get_history(self, symbol, period_days=365):
        self._maybe_fail("get_history")
        if symbol not in self.known:
            raise TickerNotFound(f"No price history for '{symbol}'. Check the symbol.", ticker=symbol)
        return make_ohlcv()

    async def get_fundamentals(self, symbol):
        self._maybe_fail("get_fundamentals")
        return Fundamentals(market_cap=3e12, pe_trailing=30.0, sector="Technology")

    async def get_news(self, symbol=None, limit=20):
        self._maybe_fail("get_news")
        return [NewsItem(title="Apple shares surge to record", publisher="Wire", url="https://example.com/1",
                         published="2026-10-20T12:00:00+00:00"),
                NewsItem(title="Chipmakers slump on tariff fears", publisher="Wire", url="https://example.com/2",
                         published="2026-10-20T11:00:00+00:00")][:limit]

    async def get_movers(self, universe, top=8):
        self._maybe_fail("get_movers")
        return {"gainers": [{"symbol": "AAPL", "change_pct": 3.0}], "losers": [{"symbol": "MSFT", "change_pct": -2.0}]}


class FakeTrader(FakeProvider, TradingProvider):
    paper = True
    live_orders_allowed = True

    def __init__(self):
        super().__init__()
        self.submitted: list[tuple] = []

    async def get_account(self):
        return {"equity": 100_000.0, "cash": 50_000.0}

    async def get_positions(self):
        return [{"symbol": "AAPL", "qty": 10}]

    async def submit_order(self, symbol, qty, side, type="market", time_in_force="day", limit_price=None):
        self.submitted.append((symbol, qty, side, type, time_in_force, limit_price))
        return {"id": "00000000-0000-0000-0000-000000000001", "symbol": symbol, "status": "accepted"}

    async def cancel_order(self, order_id):
        return None

    async def list_orders(self, status="all", limit=50):
        return []

    async def close_position(self, symbol):
        return {"symbol": symbol, "status": "accepted"}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache._data.clear()
    yield
    cache._data.clear()


def _client(monkeypatch, prov):
    monkeypatch.setattr(main, "get_provider", lambda: prov)
    monkeypatch.setattr(main, "get_trading_provider",
                        lambda: prov if isinstance(prov, TradingProvider) else None)
    main.app.dependency_overrides[main.provider] = lambda: prov
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def fake():
    return FakeProvider()


@pytest.fixture
def client(monkeypatch, fake):
    yield _client(monkeypatch, fake)
    main.app.dependency_overrides.clear()


@pytest.fixture
def trader():
    return FakeTrader()


@pytest.fixture
def trade_client(monkeypatch, trader):
    yield _client(monkeypatch, trader)
    main.app.dependency_overrides.clear()
