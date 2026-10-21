"""Stock Q&A assistant: Claude answers questions grounded in the dashboard's own data.

Claude gets read-only tools over the active MarketDataProvider (quotes, indicators,
fundamentals, news, market overview) and decides which to call per question.
Nothing here places orders. Needs ANTHROPIC_API_KEY in .env.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Literal

import anthropic
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import indicators, sentiment
from app.econ_calendar import upcoming_events
from app.providers import get_provider

log = logging.getLogger("dashboard.chat")

MAX_TOOL_ROUNDS = 8
MAX_HISTORY = 20
TICKER_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")

SYSTEM_PROMPT = """You are the research assistant built into Market Terminal, a personal stock \
analysis dashboard. The user asks about stocks, ETFs, indices and the market.

Ground every number you state in data from your tools: call get_stock_snapshot for any ticker \
you discuss (call it for several tickers in parallel when comparing), get_price_history for \
questions about price action over a period, and get_market_overview for broad market questions. \
If a tool returns an error, say what was unavailable rather than filling the gap from memory. \
Tool data is what the dashboard currently shows and may be delayed; mention the as-of time when \
it matters. Your general knowledge is fine for explaining concepts, business models and what an \
indicator means.

Be direct and concise: lead with the answer, then the supporting figures. Use short markdown \
(bold, bullet lists, small tables) where it helps. When asked whether to buy or sell, lay out \
the bull and bear case from the data and say plainly that it is analysis, not personalized \
financial advice."""

TOOLS = [
    {
        "name": "get_stock_snapshot",
        "description": (
            "Current quote, trailing returns, volatility, technical indicators with bullish/bearish "
            "signals (SMA 20/50/200, EMA, RSI, MACD, Bollinger), fundamentals and analyst targets, "
            "and recent headlines with sentiment for one ticker. Use for any question about a "
            "specific stock, ETF or index."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, e.g. AAPL, SPY, ^VIX, BRK-B"},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_price_history",
        "description": (
            "Daily closing prices and volume for one ticker over the last N calendar days "
            "(max 365). Periods over 120 days are returned as weekly closes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "days": {"type": "integer", "description": "Calendar days back, 5 to 365"},
            },
            "required": ["symbol", "days"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_market_overview",
        "description": (
            "Broad market picture: SPY/QQQ/DIA/IWM/VIX, global indices, rates, dollar, "
            "commodities, top gainers and losers among large caps, market headlines with "
            "sentiment, and upcoming US economic events."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatIn(BaseModel):
    messages: list[ChatTurn] = Field(min_length=1)
    symbol: str | None = None  # ticker the user is viewing, if any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Read at request time: main.py loads .env after importing this module.
def model() -> str:
    return os.getenv("CHAT_MODEL", "claude-opus-5-5")


def effort() -> str:
    return os.getenv("CHAT_EFFORT", "medium")  # low | medium | high | xhigh | max


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


# ---------------------------------------------------------------- tools
def _symbol(raw: str) -> str:
    s = str(raw).strip().upper()
    if not TICKER_RE.match(s):
        raise ValueError(f"'{raw}' is not a valid ticker format")
    return s


async def _snapshot(symbol: str) -> dict:
    p = get_provider()
    sym = _symbol(symbol)
    df = await p.get_history(sym, 365)
    quote, fund, news = await asyncio.gather(
        p.get_quote(sym), p.get_fundamentals(sym), p.get_news(sym, 6), return_exceptions=True)
    ind = indicators.compute_all(df)
    return {
        "symbol": sym,
        "as_of": _now(),
        "last_bar_date": df.index[-1].strftime("%Y-%m-%d"),
        "quote": quote.to_dict() if not isinstance(quote, Exception) else {"error": str(quote)},
        "performance_pct": indicators.performance(df),
        "indicators": ind["latest"],
        "signals": ind["signals"],
        "fundamentals": fund.to_dict() if not isinstance(fund, Exception) else {"error": str(fund)},
        "news": [] if isinstance(news, Exception) else [
            {"title": n.title, "publisher": n.publisher, "published": n.published,
             "sentiment": sentiment.tag(f"{n.title} {n.summary or ''}")["label"]}
            for n in news
        ],
    }


async def _history(symbol: str, days: int) -> dict:
    sym = _symbol(symbol)
    days = max(5, min(int(days), 365))
    df = await get_provider().get_history(sym, 365)
    df = df[df.index >= df.index[-1] - pd.Timedelta(days=days)]
    weekly = days > 120
    if weekly:
        df = df.resample("W-FRI").agg({"close": "last", "volume": "sum"}).dropna()
    return {
        "symbol": sym,
        "interval": "weekly" if weekly else "daily",
        "bars": [{"date": d.strftime("%Y-%m-%d"), "close": round(float(r.close), 2),
                  "volume": int(r.volume)} for d, r in df.iterrows()],
    }


def _overview_tool(tape: list[str], groups: dict[str, dict[str, str]], universe: list[str]):
    async def overview() -> dict:
        p = get_provider()
        labelled = {s: label for g in groups.values() for s, label in g.items()}
        quotes, movers, news = await asyncio.gather(
            p.get_quotes(tape + list(labelled)), p.get_movers(universe, 5), p.get_news(None, 12),
            return_exceptions=True)
        out: dict = {"as_of": _now(), "calendar": upcoming_events()[:8]}
        if isinstance(quotes, Exception):
            out["quotes_error"] = str(quotes)
        else:
            def row(s):
                q = quotes[s]
                return {"symbol": s, "label": labelled.get(s), "price": q.price, "change_pct": q.change_pct}
            out["tape"] = [row(s) for s in tape if s in quotes]
            for name, g in groups.items():
                out[name] = [row(s) for s in g if s in quotes]
        out["movers"] = {"error": str(movers)} if isinstance(movers, Exception) else movers
        out["news"] = [] if isinstance(news, Exception) else [
            {"title": n.title, "publisher": n.publisher,
             "sentiment": sentiment.tag(f"{n.title} {n.summary or ''}")["label"]} for n in news]
        return out
    return overview


# ---------------------------------------------------------------- router
def build_router(*, tape: list[str], groups: dict[str, dict[str, str]], movers_universe: list[str]) -> APIRouter:
    router = APIRouter()
    overview = _overview_tool(tape, groups, movers_universe)
    handlers = {
        "get_stock_snapshot": lambda i: _snapshot(i["symbol"]),
        "get_price_history": lambda i: _history(i["symbol"], i["days"]),
        "get_market_overview": lambda i: overview(),
    }

    async def run_tool(block) -> dict:
        try:
            result = await handlers[block.name](block.input)
            return {"type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)}
        except Exception as exc:  # noqa: BLE001 — report to Claude, let it explain
            log.warning("chat tool %s(%s) failed: %s", block.name, block.input, exc)
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": getattr(exc, "message", None) or str(exc)}

    @router.get("/api/chat/status")
    async def chat_status():
        if not enabled():
            return {"enabled": False, "reason": "Add ANTHROPIC_API_KEY to .env to enable the assistant."}
        return {"enabled": True, "model": model()}

    @router.post("/api/chat")
    async def chat(body: ChatIn):
        if not enabled():
            raise HTTPException(503, "Assistant disabled: add ANTHROPIC_API_KEY to .env")
        turns = body.messages[-MAX_HISTORY:]
        while turns and turns[0].role != "user":
            turns = turns[1:]
        if not turns or turns[-1].role != "user":
            raise HTTPException(400, "The last message must be from the user")
        messages: list = [{"role": t.role, "content": t.content} for t in turns]

        system = SYSTEM_PROMPT
        if body.symbol and TICKER_RE.match(body.symbol.strip().upper()):
            system += f"\n\nThe user is currently viewing {body.symbol.strip().upper()} in the dashboard."

        tools_used: list[str] = []
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                resp = await client().beta.messages.create(
                    model=model(),
                    max_tokens=16000,
                    system=system,
                    tools=TOOLS,
                    messages=messages,
                    output_config={"effort": effort()},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                if resp.stop_reason == "refusal":
                    return {"reply": "I can't help with that one. Try asking about a ticker or the market.",
                            "tools_used": tools_used, "model": resp.model}
                if resp.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                if resp.stop_reason != "tool_use":
                    reply = "".join(b.text for b in resp.content if b.type == "text").strip()
                    if resp.stop_reason == "max_tokens":
                        reply += "\n\n_(Answer cut off — ask me to continue.)_"
                    return {"reply": reply or "(no answer)", "tools_used": tools_used, "model": resp.model}

                calls = [b for b in resp.content if b.type == "tool_use"]
                tools_used += [f"{b.name}({b.input.get('symbol', '')})".replace("()", "") for b in calls]
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": list(await asyncio.gather(*map(run_tool, calls)))})
        except anthropic.AuthenticationError:
            raise HTTPException(502, "Anthropic rejected the API key; check ANTHROPIC_API_KEY")
        except anthropic.RateLimitError:
            raise HTTPException(429, "Assistant is rate limited; try again in a minute")
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            raise HTTPException(502, f"Assistant error: {exc.message}")
        except anthropic.APIConnectionError:
            raise HTTPException(504, "Could not reach the Claude API")

        return {"reply": "That took more lookups than I allow per question. Try narrowing it down.",
                "tools_used": tools_used, "model": model()}

    return router
