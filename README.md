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
app/chat.py                     stock Q&A assistant (Claude + read-only tools over the provider)
templates/index.html, static/app.js   frontend (watchlist is saved in browser localStorage; static/chat.js = assistant panel)
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/news` | Market news + sentiment, tape (SPY/QQQ/DIA/IWM/VIX), global indices, macro/rates, gainers/losers, econ calendar |
| `GET /api/stock/{ticker}` | Quote + bid/ask/spread, 1Y OHLCV, indicator series + signals, performance, fundamentals, analyst data, news |
| `GET /api/quotes?symbols=A,B` | Batch quotes (used by the ticker tape refresh) |
| `GET /api/chat/status` | Whether the assistant is enabled |
| `POST /api/chat` | Ask the assistant: `{messages: [{role, content}], symbol?}` -> `{reply, tools_used}` |
| `GET /api/trading/status` | Whether trading is enabled, paper vs LIVE |
| `GET /api/account` | Balances + open positions |
| `GET /api/orders?status=open\|closed\|all` | Order history |
| `POST /api/orders` | Place market/limit order (never auto-retried) |
| `DELETE /api/orders/{id}` | Cancel an open order |
| `DELETE /api/positions/{symbol}` | Market-close a whole position |

Trading endpoints need Alpaca (501 otherwise). The Trade tab (`#/trade`, or `#/trade/AAPL`) asks for
confirmation before every order, cancel and close, and warns when a sell would open a short position.
On a live account, orders are refused unless `ALPACA_ALLOW_LIVE_TRADING=yes`.

## AI trader (Trade tab)

The Trade tab has an **AI Trader** panel: type a request ("find a good entry on NVDA", "review my portfolio
and trim anything overextended"), optionally with a focus symbol. Claude reads the quote, indicator signals,
performance, headlines, your account and positions, and proposes concrete orders with its reasoning.

- **Nothing is placed automatically.** Each proposal has a *Review & place* button that opens the same
  confirmation dialog as manual orders. Proposals are single-use and expire after 15 minutes.
- **Limits are enforced on the server**, at proposal time and again just before the order is sent, whatever
  Claude suggests. Proposals that break one are shown as blocked with the reason.
- **Paper only by default.** On a LIVE Alpaca account, AI orders also need `AI_TRADER_ALLOW_LIVE=yes`
  (on top of `ALPACA_ALLOW_LIVE_TRADING=yes`); otherwise you get proposals only.

Settings (`.env`):

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Claude API key |
| `AI_TRADER_MAX_ORDER_USD` | `1000` | Max estimated value of one AI order |
| `AI_TRADER_MAX_POSITION_PCT` | `10` | Max size of a position after a buy, as % of equity |
| `AI_TRADER_MAX_ORDERS_PER_DAY` | `5` | AI orders per market day (counter resets on server restart) |
| `AI_TRADER_ALLOW_SHORTS` | `no` | `yes` lets AI sells exceed the shares you hold |
| `AI_TRADER_ALLOW_LIVE` | `no` | `yes` allows AI orders on a live account |
| `AI_TRADER_MODEL` / `AI_TRADER_EFFORT` | `claude-opus-5-5` / `high` | Model and effort level |

Endpoints: `GET /api/ai-trader/status`, `POST /api/ai-trader/propose`, `POST /api/ai-trader/proposals/{id}/execute`,
`DELETE /api/ai-trader/proposals/{id}`. Code: `app/ai_trader.py`, `static/ai_trader.js`; tests: `pytest tests`.

Non-critical sections (fundamentals, news, live quote) degrade to a `warnings[]` entry instead of failing the request.

## Stock assistant

The **Ask** button (bottom right, every page) opens a chat panel. Answers come from Claude, which
looks up the dashboard's own data before answering: quote, indicators and signals, fundamentals,
analyst targets, news sentiment, price history, and the market overview. If you're on a ticker
page, it knows which symbol you mean. It is read-only and never places orders.

Add your key to `.env` (get one at https://platform.claude.com):

```bash
ANTHROPIC_API_KEY=sk-ant-...
# optional
CHAT_MODEL=claude-opus-5-5   # default
CHAT_EFFORT=medium           # low | medium | high; lower is faster and cheaper
```

Without a key the button is dimmed and the panel says how to enable it.

## Switching to Alpaca

```bash
pip install alpaca-py
cp .env.example .env   # then fill in your keys
uvicorn main:app --reload --reload-exclude .venv
```

Nothing above the provider layer changes. For live streaming, add a WebSocket route that wraps
`alpaca.data.live.StockDataStream` and push quotes to the hero header.
