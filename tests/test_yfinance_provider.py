"""Provider-level tests with yfinance stubbed out, so failures are mapped correctly
without touching the network."""
import asyncio

import pandas as pd
import pytest

from app.errors import ProviderError, TickerNotFound
from app.providers import yfinance_provider as yfp
from tests.conftest import make_ohlcv


class StubTicker:
    history_result = None
    history_exc = None

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kwargs):
        assert kwargs.get("raise_errors") is True
        if StubTicker.history_exc:
            raise StubTicker.history_exc
        return StubTicker.history_result


@pytest.fixture
def stub_yf(monkeypatch):
    StubTicker.history_result, StubTicker.history_exc = None, None
    monkeypatch.setattr(yfp.yf, "Ticker", StubTicker)
    return StubTicker


def test_history_is_normalized(stub_yf):
    raw = make_ohlcv(30).rename(columns=str.title)
    raw["Dividends"] = 0.0
    raw.index = raw.index.tz_localize("America/New_York")
    stub_yf.history_result = raw
    df = asyncio.run(yfp.YFinanceProvider().get_history("AAPL"))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None


def test_unknown_symbol_is_404(stub_yf):
    stub_yf.history_exc = Exception("$ZZZZ: possibly delisted; no timezone found")
    with pytest.raises(TickerNotFound):
        asyncio.run(yfp.YFinanceProvider().get_history("ZZZZ"))


def test_empty_history_is_404(stub_yf):
    stub_yf.history_result = pd.DataFrame()
    with pytest.raises(TickerNotFound):
        asyncio.run(yfp.YFinanceProvider().get_history("ZZZZ"))


def test_network_failure_is_not_reported_as_unknown_ticker(stub_yf):
    stub_yf.history_exc = ConnectionError("Failed to perform, curl: (7) CONNECT tunnel failed, response 403")
    with pytest.raises(ProviderError) as ei:
        asyncio.run(yfp.YFinanceProvider().get_history("AAPL"))
    assert not isinstance(ei.value, TickerNotFound)
    assert ei.value.status_code == 502


def test_batch_quotes(monkeypatch):
    idx = pd.bdate_range(end="2026-10-20", periods=3)
    cols = pd.MultiIndex.from_product([["Close", "Volume"], ["AAPL", "MSFT"]])
    df = pd.DataFrame([[100, 200, 1e6, 2e6], [101, 198, 1e6, 2e6], [102, 196, 1e6, 2e6]],
                      index=idx, columns=cols, dtype=float)
    monkeypatch.setattr(yfp.yf, "download", lambda *a, **k: df)
    q = asyncio.run(yfp.YFinanceProvider().get_quotes(["AAPL", "MSFT"]))
    assert q["AAPL"].price == 102 and q["AAPL"].prev_close == 101
    assert q["MSFT"].change == pytest.approx(-2.0)


def test_empty_batch_quotes_is_no_result(monkeypatch):
    # yf.download returns an empty frame for unknown symbols, so this must not be a 5xx
    monkeypatch.setattr(yfp.yf, "download", lambda *a, **k: pd.DataFrame())
    assert asyncio.run(yfp.YFinanceProvider().get_quotes(["ZZZZ"])) == {}


def test_parse_news_new_and_legacy_schemas():
    new = {"content": {"title": "T1", "provider": {"displayName": "Reuters"},
                       "canonicalUrl": {"url": "https://x/1"}, "pubDate": "2026-10-20T00:00:00Z"}}
    legacy = {"title": "T2", "publisher": "AP", "link": "https://x/2", "providerPublishTime": 1760918400}
    a, b = yfp.YFinanceProvider._parse_news(new), yfp.YFinanceProvider._parse_news(legacy)
    assert (a.title, a.publisher, a.url) == ("T1", "Reuters", "https://x/1")
    assert (b.title, b.publisher, b.published) == ("T2", "AP", "2025-10-20T00:00:00+00:00")
    assert yfp.YFinanceProvider._parse_news({"content": {}}) is None
