# Market Terminal

FastAPI + Tailwind + Chart.js stock analysis dashboard. Default data source: yfinance.
Alpaca (Market Data v2 + paper trading) is a drop-in swap.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --reload-exclude .venv --port 8000
```

Run the tests (offline; a fake provider stands in for Yahoo/Alpaca):

```bash
pip install -r requirements-dev.txt
pytest
```

Open http://localhost:8000. Routes: `#/` (home) and `#/ticker/AAPL`. Press `/` to focus search.

## Layout

```
main.py                         FastAPI routes, error handlers, response shaping
app/providers/base.py           MarketDataProvider / TradingProvider contracts + Quote/Fundamentals/NewsItem
app/providers/yfinance_provider.py   current source
app/providers/alpaca_provider.py     Alpaca implementation (falls back to yfinance for indices/fundamentals)
app/providers/__init__.py       factory: picks provider from DATA_PROVIDER env var
app/resilience.py               timeout + exponential-backoff retry, error classification, TTL cache
app/errors.py                   TickerNotFound (404) / RateLimited (429) / ProviderTimeout (504)
app/indicators.py               SMA/EMA/RSI/MACD/Bollinger, signals, performance stats
app/sentiment.py                headline sentiment tagger
app/econ_calendar.py            upcoming US macro events
templates/index.html, static/app.js   frontend
tests/                          pytest suite (indicators, resilience, provider error mapping, routes)
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/news` | Market news + sentiment, tape (SPY/QQQ/DIA/IWM/VIX), global indices, macro/rates, gainers/losers, econ calendar |
| `GET /api/stock/{ticker}` | Quote + bid/ask/spread, 1Y OHLCV, indicator series + signals, performance, fundamentals, analyst data, news |
| `GET /api/quotes?symbols=A,B` | Batch quotes (used by the ticker tape refresh) |
| `GET /api/trading/status` | Whether trading is enabled, paper vs LIVE |
| `GET /api/account` | Balances + open positions |
| `GET /api/orders?status=open\|closed\|all` | Order history |
| `POST /api/orders` | Place market/limit order (never auto-retried) |
| `DELETE /api/orders/{id}` | Cancel an open order |
| `DELETE /api/positions/{symbol}` | Market-close a whole position |

Trading endpoints need Alpaca (501 otherwise). The Trade tab (`#/trade`, or `#/trade/AAPL`) asks for
confirmation before every order, cancel and close, and warns when a sell would open a short position.
On a live account, orders are refused unless `ALPACA_ALLOW_LIVE_TRADING=yes`.

Non-critical sections (fundamentals, news, live quote) degrade to a `warnings[]` entry instead of failing the request.

## Switching to Alpaca

```bash
pip install alpaca-py
cp .env.example .env   # then fill in your keys
uvicorn main:app --reload --reload-exclude .venv
```

Nothing above the provider layer changes. For live streaming, add a WebSocket route that wraps
`alpaca.data.live.StockDataStream` and push quotes to the hero header.
