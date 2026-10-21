"""Alpaca implementation (Market Data API v2 + paper Trading API).

Drop-in replacement for YFinanceProvider: set
    DATA_PROVIDER=alpaca
    ALPACA_API_KEY=...        ALPACA_SECRET_KEY=...
    ALPACA_FEED=iex           (or sip with a paid plan)
    ALPACA_PAPER=true
and `pip install alpaca-py`.

Alpaca does not serve index symbols (^VIX, ^GSPC), futures, or company
fundamentals, so those calls are delegated to a fallback provider (yfinance).
NOTE: written against alpaca-py 0.30+, not exercised without API keys.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..errors import ProviderError, TickerNotFound
from ..resilience import cached, call_with_resilience
from .base import Fundamentals, MarketDataProvider, NewsItem, Quote, TradingProvider


def _alpaca_supported(symbol: str) -> bool:
    # Plain US equity tickers only; indices (^), futures (=F), crypto (-USD), share
    # classes (BRK-B) and foreign suffixes (.SS) go to the fallback provider.
    return re.fullmatch(r"[A-Z]{1,5}", symbol) is not None


class AlpacaProvider(MarketDataProvider, TradingProvider):
    name = "alpaca"

    def __init__(self, fallback: MarketDataProvider | None = None):
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.historical.news import NewsClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("alpaca-py not installed: pip install alpaca-py") from exc

        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY must be set")
        self.feed = DataFeed(os.getenv("ALPACA_FEED", "iex"))
        self.data = StockHistoricalDataClient(key, secret)
        self.news = NewsClient(key, secret)
        self.paper = os.getenv("ALPACA_PAPER", "true").lower() != "false"
        # Live orders need a second, explicit opt-in so flipping one flag can't spend real money.
        self.live_orders_allowed = self.paper or os.getenv("ALPACA_ALLOW_LIVE_TRADING", "no").lower() == "yes"
        self.trading = TradingClient(key, secret, paper=self.paper)
        self.fallback = fallback
        try:  # fail fast on bad credentials so the factory can fall back
            self.trading.get_account()
        except Exception as exc:
            raise RuntimeError(f"Alpaca credentials rejected ({str(exc).strip()[:80]})") from exc

    # ---------- market data ----------
    async def _snapshots(self, symbols: list[str]) -> dict:
        from alpaca.data.requests import StockSnapshotRequest
        req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=self.feed)
        return await call_with_resilience(self.data.get_stock_snapshot, req, ticker=",".join(symbols))

    @staticmethod
    def _snap_to_quote(sym: str, s) -> Quote:
        price = s.latest_trade.price if s.latest_trade else (s.daily_bar.close if s.daily_bar else None)
        prev = s.previous_daily_bar.close if s.previous_daily_bar else None
        q, d = s.latest_quote, s.daily_bar
        return Quote(
            symbol=sym, price=price, prev_close=prev,
            change=price - prev if price is not None and prev else None,
            change_pct=(price - prev) / prev * 100 if price is not None and prev else None,
            bid=q.bid_price if q else None, ask=q.ask_price if q else None,
            bid_size=q.bid_size if q else None, ask_size=q.ask_size if q else None,
            volume=d.volume if d else None, day_high=d.high if d else None,
            day_low=d.low if d else None, open=d.open if d else None,
            timestamp=(s.latest_trade.timestamp.isoformat() if s.latest_trade else None),
        )

    async def get_quote(self, symbol: str) -> Quote:
        if not _alpaca_supported(symbol) and self.fallback:
            return await self.fallback.get_quote(symbol)
        snaps = await self._snapshots([symbol])
        if symbol not in snaps:
            raise TickerNotFound(f"Ticker '{symbol}' not found on Alpaca.", ticker=symbol)
        quote = self._snap_to_quote(symbol, snaps[symbol])
        if self.fallback:  # enrich name/exchange from fallback (Alpaca asset endpoint lacks long names)
            try:
                asset = await call_with_resilience(self.trading.get_asset, symbol, ticker=symbol, retries=0)
                quote.name, quote.exchange = asset.name, str(asset.exchange.value)
            except Exception:
                pass
        return quote

    @cached(ttl=15)
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        native = [s for s in symbols if _alpaca_supported(s)]
        other = [s for s in symbols if not _alpaca_supported(s)]
        out: dict[str, Quote] = {}
        if native:
            try:
                snaps = await self._snapshots(native)
                out.update({s: self._snap_to_quote(s, v) for s, v in snaps.items()})
            except ProviderError as exc:
                if not self.fallback:
                    raise
                logging.getLogger("dashboard.alpaca").warning("snapshot batch failed (%s); using fallback", exc.message)
                other = other + native
        if other and self.fallback:
            out.update(await self.fallback.get_quotes(other))
        return out

    @cached(ttl=60 * 5)
    async def get_history(self, symbol: str, period_days: int = 365) -> pd.DataFrame:
        if not _alpaca_supported(symbol) and self.fallback:
            return await self.fallback.get_history(symbol, period_days)
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=datetime.now(timezone.utc) - timedelta(days=period_days + 320),
                               adjustment=Adjustment.ALL, feed=self.feed)
        bars = await call_with_resilience(self.data.get_stock_bars, req, ticker=symbol)
        df = bars.df
        if df is None or df.empty:
            raise TickerNotFound(f"No bars for '{symbol}'.", ticker=symbol)
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = pd.to_datetime(df.index).tz_convert(None).normalize()
        return df[["open", "high", "low", "close", "volume"]]

    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        if self.fallback:
            return await self.fallback.get_fundamentals(symbol)
        return Fundamentals()

    @cached(ttl=60 * 3)
    async def get_news(self, symbol: str | None = None, limit: int = 20) -> list[NewsItem]:
        from alpaca.data.requests import NewsRequest
        req = NewsRequest(symbols=symbol, limit=min(limit, 50)) if symbol else NewsRequest(limit=min(limit, 50))
        res = await call_with_resilience(self.news.get_news, req, ticker=symbol)
        articles = res.data.get("news", []) if hasattr(res, "data") else []
        return [NewsItem(title=a.headline, publisher=a.source, url=a.url,
                         published=a.created_at.isoformat() if a.created_at else None,
                         summary=a.summary or None, tickers=list(a.symbols or []),
                         thumbnail=(a.images[0].url if getattr(a, "images", None) else None))
                for a in articles]

    @cached(ttl=60)
    async def get_movers(self, universe: list[str], top: int = 8) -> dict[str, list[dict]]:
        # Alpaca also offers /v1beta1/screener/stocks/movers — swap in here if desired.
        quotes = await self.get_quotes(universe)
        rows = sorted(({"symbol": q.symbol, "price": q.price, "change": q.change,
                        "change_pct": q.change_pct, "volume": q.volume}
                       for q in quotes.values() if q.change_pct is not None),
                      key=lambda r: r["change_pct"], reverse=True)
        return {"gainers": rows[:top], "losers": list(reversed(rows[-top:]))}

    # ---------- paper trading ----------
    async def _trade_call(self, fn, *args, ticker: str | None = None):
        """Run a trading call, turning Alpaca API rejections into readable 4xx errors."""
        from alpaca.common.exceptions import APIError
        try:
            return await call_with_resilience(fn, *args, retries=0, ticker=ticker)
        except ProviderError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, APIError):
                exc = self._api_error(cause, ticker)
            raise exc

    @staticmethod
    def _api_error(e, ticker) -> ProviderError:
        try:
            msg = e.message
        except Exception:
            msg = str(e)
        err = ProviderError(f"Alpaca rejected the request: {msg}", ticker=ticker)
        status = getattr(e, "status_code", None) or 422
        err.status_code = status if 400 <= status < 500 else 502
        err.code = "order_rejected"
        return err

    @staticmethod
    def _f(v):
        return float(v) if v is not None else None

    async def get_account(self) -> dict:
        a = await self._trade_call(self.trading.get_account)
        equity, last = float(a.equity), float(a.last_equity)
        return {"equity": equity, "last_equity": last, "day_pl": equity - last,
                "day_pl_pct": (equity / last - 1) * 100 if last else None,
                "cash": float(a.cash), "buying_power": float(a.buying_power),
                "long_market_value": self._f(a.long_market_value),
                "short_market_value": self._f(a.short_market_value),
                "daytrade_count": a.daytrade_count, "status": str(a.status.value),
                "pattern_day_trader": a.pattern_day_trader,
                "trading_blocked": a.trading_blocked,
                "mode": "paper" if self.paper else "LIVE", "live_orders_allowed": self.live_orders_allowed}

    async def get_positions(self) -> list[dict]:
        ps = await self._trade_call(self.trading.get_all_positions)
        f = self._f
        return [{"symbol": p.symbol, "side": str(p.side.value), "qty": f(p.qty),
                 "avg_entry": f(p.avg_entry_price), "current_price": f(p.current_price),
                 "market_value": f(p.market_value), "cost_basis": f(p.cost_basis),
                 "unrealized_pl": f(p.unrealized_pl),
                 "unrealized_plpc": f(p.unrealized_plpc) * 100 if p.unrealized_plpc is not None else None,
                 "change_today_pct": f(p.change_today) * 100 if p.change_today is not None else None}
                for p in ps]

    def _order_dict(self, o) -> dict:
        f = self._f
        return {"id": str(o.id), "symbol": o.symbol, "side": str(o.side.value),
                "qty": f(o.qty), "notional": f(o.notional), "filled_qty": f(o.filled_qty),
                "type": str(o.type.value), "time_in_force": str(o.time_in_force.value),
                "limit_price": f(o.limit_price), "filled_avg_price": f(o.filled_avg_price),
                "status": str(o.status.value),
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
                "filled_at": o.filled_at.isoformat() if o.filled_at else None}

    async def list_orders(self, status: str = "all", limit: int = 50) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus(status), limit=limit, direction="desc")
        orders = await self._trade_call(self.trading.get_orders, req)
        return [self._order_dict(o) for o in orders]

    def _check_live(self):
        if not self.live_orders_allowed:
            err = ProviderError("Live account detected: order blocked. Set ALPACA_ALLOW_LIVE_TRADING=yes to enable.")
            err.status_code, err.code = 403, "live_trading_disabled"
            raise err

    async def submit_order(self, symbol, qty, side, type="market", time_in_force="day", limit_price=None) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        self._check_live()
        common = dict(symbol=symbol, qty=qty, side=OrderSide(side.lower()),
                      time_in_force=TimeInForce(time_in_force.lower()))
        if type == "limit":
            if limit_price is None:
                raise ProviderError("limit_price required for limit orders")
            req = LimitOrderRequest(limit_price=limit_price, **common)
        else:
            req = MarketOrderRequest(**common)
        o = await self._trade_call(self.trading.submit_order, req, ticker=symbol)
        return self._order_dict(o)

    async def cancel_order(self, order_id: str) -> None:
        await self._trade_call(self.trading.cancel_order_by_id, order_id)

    async def close_position(self, symbol: str) -> dict:
        self._check_live()
        o = await self._trade_call(self.trading.close_position, symbol, ticker=symbol)
        return self._order_dict(o)
