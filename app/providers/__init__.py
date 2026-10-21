"""Provider factory — the ONLY place that decides which data source is live."""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from .base import MarketDataProvider, TradingProvider

log = logging.getLogger("dashboard.providers")


@lru_cache(maxsize=1)
def get_provider() -> MarketDataProvider:
    choice = os.getenv("DATA_PROVIDER", "yfinance").lower()
    from .yfinance_provider import YFinanceProvider

    if choice == "alpaca":
        from .alpaca_provider import AlpacaProvider
        try:
            return AlpacaProvider(fallback=YFinanceProvider())
        except RuntimeError as exc:
            log.error("Alpaca unavailable (%s); falling back to yfinance", exc)
    return YFinanceProvider()


def get_trading_provider() -> TradingProvider | None:
    p = get_provider()
    return p if isinstance(p, TradingProvider) else None


__all__ = ["get_provider", "get_trading_provider", "MarketDataProvider", "TradingProvider"]
