"""Yahoo Finance implementation of MarketDataProvider (current default source)."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from ..errors import TickerNotFound
from ..resilience import cache, cached, call_with_resilience
from .base import Fundamentals, MarketDataProvider, NewsItem, Quote

log = logging.getLogger("dashboard.yfinance")

MARKET_NEWS_SOURCES = ["^GSPC", "SPY", "QQQ", "^DJI", "^TNX", "CL=F"]
# Search queries used when Yahoo's per-ticker news endpoint returns nothing.
MARKET_NEWS_QUERIES = ["stock market", "S&P 500", "Federal Reserve", "Treasury yields", "earnings", "oil prices"]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _pct(a, b):
    return (a - b) / b * 100 if a is not None and b not in (None, 0) else None


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    # ---------- internal helpers ----------
    async def _info(self, symbol: str) -> dict:
        key = f"yf.info:{symbol}"
        hit = cache.get(key)
        if hit is not None:
            return hit

        def fetch():
            try:
                return yf.Ticker(symbol).info or {}
            except Exception as exc:  # yfinance raises bare HTTP errors for bad symbols
                if "404" in str(exc):
                    return {}
                raise

        info = await call_with_resilience(fetch, ticker=symbol)
        cache.set(key, info, 60)
        return info

    # ---------- MarketDataProvider ----------
    @cached(ttl=60 * 15)
    async def get_history(self, symbol: str, period_days: int = 365) -> pd.DataFrame:
        # Pull extra lookback so the 200-day SMA is populated across the full window.
        start = (datetime.now(timezone.utc) - timedelta(days=period_days + 320)).date()

        def fetch():
            # raise_errors: otherwise yfinance swallows network failures and returns an empty
            # frame, which we would misreport as an unknown ticker (404).
            return yf.Ticker(symbol).history(start=str(start), interval="1d", auto_adjust=False,
                                             raise_errors=True)

        df = await call_with_resilience(fetch, ticker=symbol)
        if df is None or df.empty:
            raise TickerNotFound(f"No price history for '{symbol}'. Check the symbol.", ticker=symbol)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        return df

    async def get_quote(self, symbol: str) -> Quote:
        info = await self._info(symbol)
        price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
        prev = _num(info.get("regularMarketPreviousClose") or info.get("previousClose"))

        if price is None:  # thin/unknown info payload: fall back to fast_info
            def fast():
                fi = yf.Ticker(symbol).fast_info
                return {"last": fi.get("lastPrice"), "prev": fi.get("previousClose"),
                        "vol": fi.get("lastVolume"), "hi": fi.get("dayHigh"), "lo": fi.get("dayLow"),
                        "open": fi.get("open"), "cur": fi.get("currency")}
            try:
                f = await call_with_resilience(fast, ticker=symbol)
            except Exception:
                f = {}
            price, prev = _num(f.get("last")), _num(f.get("prev"))
            if price is None:
                raise TickerNotFound(f"No quote available for '{symbol}'.", ticker=symbol)
            info = {**info, "regularMarketVolume": f.get("vol"), "dayHigh": f.get("hi"),
                    "dayLow": f.get("lo"), "open": f.get("open"), "currency": f.get("cur") or "USD"}

        change = price - prev if prev is not None else None
        bid, ask = _num(info.get("bid")), _num(info.get("ask"))
        return Quote(
            symbol=symbol.upper(),
            price=price,
            prev_close=prev,
            change=change,
            change_pct=_pct(price, prev),
            bid=bid or None,  # Yahoo reports 0 outside RTH
            ask=ask or None,
            bid_size=_num(info.get("bidSize")),
            ask_size=_num(info.get("askSize")),
            volume=_num(info.get("regularMarketVolume") or info.get("volume")),
            avg_volume=_num(info.get("averageVolume")),
            day_high=_num(info.get("dayHigh") or info.get("regularMarketDayHigh")),
            day_low=_num(info.get("dayLow") or info.get("regularMarketDayLow")),
            open=_num(info.get("open") or info.get("regularMarketOpen")),
            currency=info.get("currency") or "USD",
            exchange=info.get("fullExchangeName") or info.get("exchange"),
            name=info.get("longName") or info.get("shortName"),
            market_state=info.get("marketState"),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @cached(ttl=60)
    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Batch quotes from daily bars — one HTTP round trip for the whole list."""
        syms = list(dict.fromkeys(symbols))

        def fetch():
            return yf.download(syms, period="5d", interval="1d", group_by="column",
                               auto_adjust=False, progress=False, threads=True)

        df = await call_with_resilience(fetch, ticker=",".join(syms))
        out: dict[str, Quote] = {}
        if df is None or df.empty:  # all symbols unknown, or Yahoo unreachable: yf.download can't tell us which
            return out
        close = df["Close"] if "Close" in df else pd.DataFrame()
        vol = df["Volume"] if "Volume" in df else pd.DataFrame()
        if isinstance(close, pd.Series):  # single symbol
            close, vol = close.to_frame(syms[0]), vol.to_frame(syms[0])
        for s in syms:
            if s not in close:
                continue
            c = close[s].dropna()
            if c.empty:
                continue
            price = _num(c.iloc[-1])
            prev = _num(c.iloc[-2]) if len(c) > 1 else None
            v = vol[s].dropna() if s in vol else pd.Series(dtype=float)
            out[s] = Quote(symbol=s, price=price, prev_close=prev,
                           change=price - prev if prev is not None else None,
                           change_pct=_pct(price, prev),
                           volume=_num(v.iloc[-1]) if len(v) else None,
                           timestamp=str(c.index[-1].date()))
        return out

    @cached(ttl=60 * 30)
    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        info = await self._info(symbol)
        price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
        div_rate = _num(info.get("dividendRate") or info.get("trailingAnnualDividendRate"))
        # Compute yield ourselves: Yahoo changed `dividendYield` units in 2025.
        div_yield = div_rate / price * 100 if div_rate and price else None

        analyst = {
            "rating": info.get("recommendationKey"),
            "mean": _num(info.get("recommendationMean")),  # 1=Strong Buy .. 5=Sell
            "count": info.get("numberOfAnalystOpinions"),
            "target_mean": _num(info.get("targetMeanPrice")),
            "target_median": _num(info.get("targetMedianPrice")),
            "target_high": _num(info.get("targetHighPrice")),
            "target_low": _num(info.get("targetLowPrice")),
            "upside_pct": _pct(_num(info.get("targetMeanPrice")), price),
            "breakdown": None,
        }
        try:
            rec = await call_with_resilience(lambda: yf.Ticker(symbol).recommendations, ticker=symbol, retries=0)
            if isinstance(rec, pd.DataFrame) and not rec.empty:
                row = rec.iloc[0]
                analyst["breakdown"] = {k: int(row[k]) for k in
                                        ("strongBuy", "buy", "hold", "sell", "strongSell") if k in row}
        except Exception as exc:  # optional enrichment
            log.info("recommendations unavailable for %s: %s", symbol, exc)

        def pct(key):
            v = _num(info.get(key))
            return v * 100 if v is not None else None

        return Fundamentals(
            market_cap=_num(info.get("marketCap")),
            pe_trailing=_num(info.get("trailingPE")),
            pe_forward=_num(info.get("forwardPE")),
            peg=_num(info.get("trailingPegRatio") or info.get("pegRatio")),
            price_to_book=_num(info.get("priceToBook")),
            price_to_sales=_num(info.get("priceToSalesTrailing12Months")),
            eps_trailing=_num(info.get("trailingEps")),
            eps_forward=_num(info.get("forwardEps")),
            dividend_yield=div_yield,
            payout_ratio=pct("payoutRatio"),
            beta=_num(info.get("beta")),
            week52_high=_num(info.get("fiftyTwoWeekHigh")),
            week52_low=_num(info.get("fiftyTwoWeekLow")),
            short_ratio=_num(info.get("shortRatio")),
            short_pct_float=pct("shortPercentOfFloat"),
            shares_outstanding=_num(info.get("sharesOutstanding")),
            float_shares=_num(info.get("floatShares")),
            profit_margin=pct("profitMargins"),
            roe=pct("returnOnEquity"),
            debt_to_equity=_num(info.get("debtToEquity")),
            revenue_ttm=_num(info.get("totalRevenue")),
            sector=info.get("sector"),
            industry=info.get("industry"),
            analyst=analyst,
        )

    @staticmethod
    def _parse_news(raw: dict) -> NewsItem | None:
        c = raw.get("content")
        if isinstance(c, dict):  # yfinance >= 0.2.48 schema
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
            thumb = None
            res = ((c.get("thumbnail") or {}).get("resolutions") or [])
            if res:
                thumb = sorted(res, key=lambda r: r.get("width", 0))[0].get("url")
            title = c.get("title")
            if not title:
                return None
            return NewsItem(title=title, publisher=(c.get("provider") or {}).get("displayName"),
                            url=url, published=c.get("pubDate") or c.get("displayTime"),
                            summary=c.get("summary") or c.get("description"),
                            tickers=[], thumbnail=thumb)
        if raw.get("title"):  # legacy / yf.Search schema
            ts = raw.get("providerPublishTime")
            res = ((raw.get("thumbnail") or {}).get("resolutions") or [])
            thumb = sorted(res, key=lambda r: r.get("width", 0))[0].get("url") if res else None
            return NewsItem(title=raw["title"], publisher=raw.get("publisher"), url=raw.get("link"),
                            published=datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                            tickers=raw.get("relatedTickers") or [], thumbnail=thumb)
        return None

    @staticmethod
    def _fetch_news(source: str, *, search: bool, count: int) -> list[dict]:
        if search:
            return yf.Search(source, news_count=count, max_results=1).news or []
        return yf.Ticker(source).news or []

    @cached(ttl=60 * 5)
    async def get_news(self, symbol: str | None = None, limit: int = 20) -> list[NewsItem]:
        items: dict[str, NewsItem] = {}
        keywords: set[str] = set()
        if symbol:  # used to discard Yahoo's loosely-tagged earnings-call boilerplate
            keywords = {symbol.lower()}
            try:
                info = await self._info(symbol)
                name = (info.get("shortName") or info.get("longName") or "").split()
                if name:
                    keywords.add(name[0].strip(",.").lower())
            except Exception:
                pass
        plans = [([symbol], False), ([symbol], True)] if symbol else \
                [(MARKET_NEWS_SOURCES, False), (MARKET_NEWS_QUERIES, True)]
        for sources, search in plans:
            if items:  # first strategy produced results; skip the fallback
                break
            raws = await asyncio.gather(*(
                call_with_resilience(self._fetch_news, s, search=search, count=limit * 2, ticker=s, retries=1)
                for s in sources), return_exceptions=True)
            for s, raw in zip(sources, raws):
                if isinstance(raw, Exception):
                    log.warning("news fetch failed for %s: %s", s, raw)
                    continue
                self._merge_news(items, raw, symbol, keywords)
        return sorted(items.values(), key=lambda n: n.published or "", reverse=True)[:limit]

    def _merge_news(self, items: dict[str, NewsItem], raw: list[dict], symbol: str | None,
                    keywords: set[str]) -> None:
        for r in raw:
            n = self._parse_news(r)
            if not n or n.title in items:
                continue
            title = n.title.lower()
            # Yahoo floods feeds with templated call transcripts; keep only the ticker's own.
            if "earnings call" in title and not any(k in title for k in keywords):
                continue
            if symbol and not n.tickers:
                n.tickers = [symbol.upper()]
            items[n.title] = n

    @cached(ttl=60 * 2)
    async def get_movers(self, universe: list[str], top: int = 8) -> dict[str, list[dict]]:
        quotes = await self.get_quotes(universe)
        rows = [{"symbol": q.symbol, "price": q.price, "change": q.change,
                 "change_pct": q.change_pct, "volume": q.volume}
                for q in quotes.values() if q.change_pct is not None]
        rows.sort(key=lambda r: r["change_pct"], reverse=True)
        return {"gainers": rows[:top], "losers": list(reversed(rows[-top:]))}
