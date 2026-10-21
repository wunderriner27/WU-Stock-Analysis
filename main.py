"""Market Terminal — FastAPI entrypoint.

Run:  uvicorn main:app --reload
Swap data source:  DATA_PROVIDER=alpaca (see app/providers/alpaca_provider.py)
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import ai_trader, chat, indicators, sentiment
from app.econ_calendar import upcoming_events
from app.errors import ProviderError, RateLimited
from app.providers import MarketDataProvider, get_provider, get_trading_provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dashboard")

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")  # API keys live here, never in source
app = FastAPI(title="Market Terminal", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
app.include_router(ai_trader.router)

TICKER_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")
TAPE = ["SPY", "QQQ", "DIA", "IWM", "^VIX"]
GLOBAL_INDICES = {
    "^GSPC": "S&P 500", "^IXIC": "Nasdaq Comp", "^DJI": "Dow Jones", "^RUT": "Russell 2000",
    "^FTSE": "FTSE 100", "^GDAXI": "DAX", "^FCHI": "CAC 40", "^N225": "Nikkei 225",
    "^HSI": "Hang Seng", "000001.SS": "Shanghai",
}
MACRO = {
    "^TNX": "US 10Y Yield", "^FVX": "US 5Y Yield", "^IRX": "US 13W T-Bill",
    "DX-Y.NYB": "US Dollar Idx", "CL=F": "WTI Crude", "GC=F": "Gold", "HG=F": "Copper",
    "BTC-USD": "Bitcoin",
}
MOVERS_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B", "JPM", "LLY", "V",
    "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ", "NFLX", "AMD", "CRM", "ORCL", "ADBE", "BAC",
    "KO", "PEP", "WMT", "CVX", "MRK", "ABBV", "INTC", "QCOM", "DIS", "CSCO", "PLTR", "UBER",
    "COIN", "SHOP", "MU", "BA", "GS", "CAT", "NKE", "PYPL", "SBUX", "SMCI", "ARM",
]
app.include_router(chat.build_router(tape=TAPE, groups={"indices": GLOBAL_INDICES, "macro": MACRO},
                                      movers_universe=MOVERS_UNIVERSE))


# ---------------------------------------------------------------- error handling
@app.exception_handler(ProviderError)
async def provider_error_handler(_: Request, exc: ProviderError):
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
    return JSONResponse(status_code=exc.status_code, headers=headers,
                        content={"error": exc.code, "message": exc.message, "ticker": exc.ticker})


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"error": "internal_error", "message": str(exc)})


def provider() -> MarketDataProvider:
    return get_provider()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _soft(coro, default, warnings: list[str], label: str):
    """Await a non-critical section; on failure record a warning and degrade."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        log.warning("%s unavailable: %s", label, exc)
        warnings.append(f"{label} unavailable: {getattr(exc, 'message', str(exc))}")
        return default


def _news_dicts(items) -> list[dict]:
    return [{**n.to_dict(), "sentiment": sentiment.tag(f"{n.title} {n.summary or ''}")} for n in items]


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"provider": get_provider().name})


# ---------------------------------------------------------------- API
@app.get("/api/health")
async def health():
    return {"status": "ok", "provider": get_provider().name, "time": _now()}


@app.get("/api/quotes")
async def quotes(symbols: str = Query(..., description="Comma-separated"), p: MarketDataProvider = Depends(provider)):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
    if not syms or not all(TICKER_RE.match(s) for s in syms):
        raise HTTPException(400, "Invalid symbol list")
    q = await p.get_quotes(syms)
    return {"quotes": {k: v.to_dict() for k, v in q.items()}, "generated_at": _now()}


@app.get("/api/news")
async def market_news(p: MarketDataProvider = Depends(provider)):
    warnings: list[str] = []
    all_syms = TAPE + list(GLOBAL_INDICES) + list(MACRO)
    news, quotes_, movers = await asyncio.gather(
        _soft(p.get_news(None, 30), [], warnings, "Market news"),
        _soft(p.get_quotes(all_syms), {}, warnings, "Index quotes"),
        _soft(p.get_movers(MOVERS_UNIVERSE, 8), {"gainers": [], "losers": []}, warnings, "Movers"),
    )

    def rows(mapping):
        return [{**quotes_[s].to_dict(), "label": label} for s, label in mapping.items() if s in quotes_]

    news_items = _news_dicts(news)
    tally = {k: sum(n["sentiment"]["label"] == k for n in news_items) for k in ("bullish", "bearish", "neutral")}
    return {
        "provider": p.name,
        "tape": [quotes_[s].to_dict() for s in TAPE if s in quotes_],
        "indices": rows(GLOBAL_INDICES),
        "macro": rows(MACRO),
        "movers": movers,
        "calendar": upcoming_events(),
        "news": news_items,
        "news_sentiment": tally,
        "warnings": warnings,
        "generated_at": _now(),
    }


@app.get("/api/stock/{ticker}")
async def stock(ticker: str, p: MarketDataProvider = Depends(provider)):
    symbol = ticker.strip().upper()
    if not TICKER_RE.match(symbol):
        raise HTTPException(400, f"'{ticker}' is not a valid ticker format")

    # History is the critical path: if it fails (e.g. unknown ticker) the request fails.
    warnings: list[str] = []
    history_task = asyncio.create_task(p.get_history(symbol, 365))
    quote_task = asyncio.create_task(p.get_quote(symbol))
    fund_task = asyncio.create_task(_soft(p.get_fundamentals(symbol), None, warnings, "Fundamentals"))
    news_task = asyncio.create_task(_soft(p.get_news(symbol, 15), [], warnings, "Ticker news"))
    try:
        df: pd.DataFrame = await history_task
    except Exception:
        for t in (quote_task, fund_task, news_task):
            t.cancel()
        raise

    try:
        quote = (await quote_task).to_dict()
    except ProviderError as exc:  # synthesize from last bar so the page still renders
        warnings.append(f"Live quote unavailable ({exc.code}); showing last close")
        last, prev = df["close"].iloc[-1], df["close"].iloc[-2] if len(df) > 1 else None
        quote = {"symbol": symbol, "price": float(last), "prev_close": float(prev) if prev else None,
                 "change": float(last - prev) if prev else None,
                 "change_pct": float((last / prev - 1) * 100) if prev else None,
                 "volume": float(df["volume"].iloc[-1]), "bid": None, "ask": None, "spread": None}
    fundamentals, news = await fund_task, await news_task

    ind = indicators.compute_all(df)
    window = df.index >= df.index[-1] - pd.Timedelta(days=365)
    n = int(window.sum())
    hist = df[window]

    return {
        "symbol": symbol,
        "provider": p.name,
        "quote": quote,
        "history": {
            "dates": [d.strftime("%Y-%m-%d") for d in hist.index],
            **{c: indicators.series_to_list(hist[c]) for c in ("open", "high", "low", "close", "volume")},
        },
        "indicators": {
            "series": {k: v[-n:] for k, v in ind["series"].items()},
            "latest": ind["latest"],
            "signals": ind["signals"],
        },
        "performance": indicators.performance(df),
        "fundamentals": fundamentals.to_dict() if fundamentals else None,
        "news": _news_dicts(news),
        "warnings": warnings,
        "generated_at": _now(),
    }


# ---------------------------------------------------------------- paper trading (Alpaca)
class OrderIn(BaseModel):
    symbol: str
    qty: float = Field(gt=0)
    side: str = Field(pattern="^(buy|sell)$")
    type: str = Field("market", pattern="^(market|limit)$")
    time_in_force: str = "day"
    limit_price: float | None = None


def trader():
    t = get_trading_provider()
    if t is None:
        raise HTTPException(501, "Trading requires DATA_PROVIDER=alpaca with paper credentials")
    return t


@app.get("/api/trading/status")
async def trading_status():
    """Never errors: lets the UI decide whether to show the Trade tab as enabled."""
    t = get_trading_provider()
    if t is None:
        return {"enabled": False, "reason": "Set DATA_PROVIDER=alpaca with API keys in .env to enable trading."}
    return {"enabled": True, "mode": "paper" if t.paper else "LIVE", "live_orders_allowed": t.live_orders_allowed}


@app.get("/api/account")
async def account(t=Depends(trader)):
    acct, positions = await asyncio.gather(t.get_account(), t.get_positions())
    return {"account": acct, "positions": positions, "generated_at": _now()}


@app.get("/api/orders")
async def list_orders(status: str = Query("all", pattern="^(open|closed|all)$"),
                      limit: int = Query(50, ge=1, le=500), t=Depends(trader)):
    return {"orders": await t.list_orders(status, limit)}


@app.post("/api/orders")
async def place_order(order: OrderIn, t=Depends(trader)):
    symbol = order.symbol.strip().upper()
    if not TICKER_RE.match(symbol):
        raise HTTPException(400, f"'{order.symbol}' is not a valid ticker")
    if order.type == "limit" and (order.limit_price is None or order.limit_price <= 0):
        raise HTTPException(400, "Limit orders need a positive limit price")
    if order.time_in_force not in ("day", "gtc"):
        raise HTTPException(400, "time_in_force must be 'day' or 'gtc'")
    if order.qty != int(order.qty) and (order.type != "market" or order.time_in_force != "day"):
        raise HTTPException(400, "Fractional shares are only allowed on market DAY orders")
    log.info("ORDER %s %s %s %s tif=%s limit=%s", order.side, order.qty, symbol, order.type,
             order.time_in_force, order.limit_price)
    return await t.submit_order(symbol, order.qty, order.side, order.type,
                                order.time_in_force, order.limit_price)


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str, t=Depends(trader)):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", order_id):
        raise HTTPException(400, "Invalid order id")
    await t.cancel_order(order_id)
    return {"cancelled": order_id}


@app.delete("/api/positions/{symbol}")
async def close_position(symbol: str, t=Depends(trader)):
    symbol = symbol.strip().upper()
    if not TICKER_RE.match(symbol):
        raise HTTPException(400, "Invalid symbol")
    log.info("CLOSE POSITION %s", symbol)
    return await t.close_position(symbol)
