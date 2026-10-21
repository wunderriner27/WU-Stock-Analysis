"""The data-provider contract.

Everything above this layer (indicators, routes, frontend) consumes ONLY these
normalized shapes. To move to Alpaca, implement `MarketDataProvider` (and
optionally `TradingProvider`) in a single class and set DATA_PROVIDER=alpaca.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class Quote:
    symbol: str
    price: Optional[float]
    prev_close: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    open: Optional[float] = None
    currency: str = "USD"
    exchange: Optional[str] = None
    name: Optional[str] = None
    market_state: Optional[str] = None
    timestamp: Optional[str] = None

    @property
    def spread(self) -> Optional[float]:
        if self.bid and self.ask and self.ask >= self.bid:
            return round(self.ask - self.bid, 4)
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["spread"] = self.spread
        mid = (self.bid + self.ask) / 2 if self.bid and self.ask else None
        d["spread_bps"] = round(self.spread / mid * 10_000, 2) if self.spread is not None and mid else None
        return d


@dataclass
class Fundamentals:
    market_cap: Optional[float] = None
    pe_trailing: Optional[float] = None
    pe_forward: Optional[float] = None
    peg: Optional[float] = None
    price_to_book: Optional[float] = None
    price_to_sales: Optional[float] = None
    eps_trailing: Optional[float] = None
    eps_forward: Optional[float] = None
    dividend_yield: Optional[float] = None  # as percent, e.g. 0.52 == 0.52%
    payout_ratio: Optional[float] = None
    beta: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    short_ratio: Optional[float] = None
    short_pct_float: Optional[float] = None
    shares_outstanding: Optional[float] = None
    float_shares: Optional[float] = None
    profit_margin: Optional[float] = None
    roe: Optional[float] = None
    debt_to_equity: Optional[float] = None
    revenue_ttm: Optional[float] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    analyst: dict = field(default_factory=dict)  # rating, mean, targets, breakdown

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsItem:
    title: str
    publisher: Optional[str]
    url: Optional[str]
    published: Optional[str]  # ISO-8601
    summary: Optional[str] = None
    tickers: list[str] = field(default_factory=list)
    thumbnail: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class MarketDataProvider(ABC):
    """Read-only market data. All methods are async; implementations must raise
    errors from app.errors (TickerNotFound / ProviderTimeout / RateLimited)."""

    name: str = "abstract"

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    async def get_history(self, symbol: str, period_days: int = 365) -> pd.DataFrame:
        """Daily bars. Index: tz-naive DatetimeIndex. Columns: open, high, low, close, volume."""

    @abstractmethod
    async def get_fundamentals(self, symbol: str) -> Fundamentals: ...

    @abstractmethod
    async def get_news(self, symbol: str | None = None, limit: int = 20) -> list[NewsItem]:
        """Ticker news when symbol is given, general market news otherwise."""

    @abstractmethod
    async def get_movers(self, universe: list[str], top: int = 8) -> dict[str, list[dict]]:
        """{'gainers': [...], 'losers': [...]}"""


class TradingProvider(ABC):
    """Order execution surface (paper trading). Implemented by Alpaca later."""

    @abstractmethod
    async def get_account(self) -> dict: ...

    @abstractmethod
    async def get_positions(self) -> list[dict]: ...

    @abstractmethod
    async def submit_order(self, symbol: str, qty: float, side: str, type: str = "market",
                           time_in_force: str = "day", limit_price: float | None = None) -> dict: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def list_orders(self, status: str = "all", limit: int = 50) -> list[dict]:
        """status: open | closed | all"""

    @abstractmethod
    async def close_position(self, symbol: str) -> dict:
        """Market-close the whole position; returns the resulting order."""
