"""AI trader auto mode: Claude plans and places trades on a schedule, without a confirm click.

Guard rails (on top of every limit in app/ai_trader.py, which still applies to each order):
  - Off by default and must be allowed with AI_TRADER_AUTO=yes before it can be switched on.
  - Paper accounts only. If the account is LIVE the loop stops, whatever the other settings say.
  - Runs only during regular US market hours (Mon-Fri 9:30-16:00 ET).
  - Stops itself after 3 runs in a row fail, and is always off after a server restart.

Each run reuses the manual flow: ai_trader.propose() for a plan, then ai_trader.execute() for each
proposed order that passed the limits, so the two modes can't drift apart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from datetime import datetime, time as dtime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import ai_trader

log = logging.getLogger("dashboard.ai_autopilot")
router = APIRouter(prefix="/api/ai-trader/auto", tags=["ai-trader"])

MAX_SYMBOLS = 5
MAX_ERRORS = 3
MARKET_OPEN, MARKET_CLOSE = dtime(9, 30), dtime(16, 0)
AUTO_NOTE = ("\n\nAuto mode is on: any order you propose that passes the limits will be placed immediately, "
             "with no human review. Only propose trades you would be comfortable having executed unattended.")

_state: dict = {"enabled": False, "instruction": "", "symbols": [], "interval_min": 60,
                "next_run": None, "last_run": None, "errors": 0, "stopped_reason": None, "running": False}
_activity: deque = deque(maxlen=50)
_task: asyncio.Task | None = None


def allowed() -> bool:
    return os.getenv("AI_TRADER_AUTO", "no").lower() == "yes"


def market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ai_trader.MARKET_TZ)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE


def _now() -> datetime:
    return datetime.now(ai_trader.MARKET_TZ)


def _stop(reason: str) -> None:
    # The loop notices within 30s and exits. It isn't cancelled, so a run in progress can't be
    # cut off between placing an order and counting it toward the daily limit.
    _state.update(enabled=False, next_run=None, stopped_reason=reason)
    log.warning("Auto mode stopped: %s", reason)


async def run_once() -> dict:
    """One auto-mode pass over the configured symbols (or the whole portfolio)."""
    t = ai_trader.get_trading_provider()
    if t is None:
        raise RuntimeError("Trading is not enabled (DATA_PROVIDER=alpaca)")
    if not t.paper:
        _stop("Account is LIVE; auto mode only runs on paper accounts")
        raise RuntimeError(_state["stopped_reason"])

    entry = {"at": _now().isoformat(timespec="seconds"), "plans": [], "placed": [], "blocked": [], "error": None}
    _state["running"] = True
    try:
        for sym in _state["symbols"] or [None]:
            plan = await ai_trader.propose(ai_trader.ProposeIn(
                instruction=_state["instruction"] + AUTO_NOTE, symbol=sym))
            entry["plans"].append({"symbol": sym, "summary": plan["summary"]})
            for o in plan["orders"]:
                label = f"{o['side'].upper()} {o['qty']} {o['symbol']}"
                if o["violations"]:
                    ai_trader._proposals.pop(o["id"], None)
                    entry["blocked"].append({"order": label, "reason": "; ".join(o["violations"])})
                    continue
                try:
                    r = await ai_trader.execute(o["id"])
                    entry["placed"].append({"order": label, "status": r["order"].get("status"),
                                            "rationale": o["rationale"]})
                except HTTPException as exc:  # re-check at execution time failed
                    entry["blocked"].append({"order": label, "reason": str(exc.detail)})
        _state["errors"] = 0
    except Exception as exc:  # noqa: BLE001 - recorded in the activity log, counted toward the stop
        entry["error"] = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _state["errors"] += 1
        if _state["errors"] >= MAX_ERRORS and _state["enabled"]:
            _stop(f"{MAX_ERRORS} runs in a row failed; last error: {entry['error']}")
    finally:
        _state["running"] = False
        _state["last_run"] = entry["at"]
        _activity.appendleft(entry)
    log.info("Auto run: %d placed, %d blocked, error=%s", len(entry["placed"]), len(entry["blocked"]), entry["error"])
    return entry


async def _loop() -> None:
    while _state["enabled"]:
        nxt = _state["next_run"]
        if nxt and _now() < nxt:
            await asyncio.sleep(min((nxt - _now()).total_seconds(), 30))
            continue
        _state["next_run"] = _now() + timedelta(minutes=_state["interval_min"])
        if not market_open() or _state["running"]:
            continue
        try:
            await run_once()
        except Exception:  # noqa: BLE001 - run_once already recorded it
            pass


def _public() -> dict:
    s = {k: v for k, v in _state.items()}
    s["next_run"] = s["next_run"].isoformat(timespec="seconds") if s["next_run"] else None
    return {**s, "allowed": allowed(), "market_open": market_open(), "activity": list(_activity)}


# ---------------------------------------------------------------- routes
class AutoIn(BaseModel):
    enabled: bool
    instruction: str = Field("", max_length=1500)
    symbols: list[str] = Field(default_factory=list)
    interval_min: int = Field(60, ge=15, le=24 * 60)


@router.get("")
async def get_auto():
    return _public()


@router.post("")
async def set_auto(body: AutoIn):
    global _task
    if not body.enabled:
        if _state["enabled"]:
            _stop("Turned off")
        return _public()

    if not allowed():
        raise HTTPException(403, "Auto mode is disabled. Set AI_TRADER_AUTO=yes in .env and restart to allow it.")
    t = ai_trader._trader()
    if not t.paper:
        raise HTTPException(403, "Auto mode only runs on paper accounts.")
    symbols = list(dict.fromkeys(s.strip().upper() for s in body.symbols if s.strip()))
    bad = [s for s in symbols if not re.fullmatch(r"[A-Z]{1,5}", s)]
    if bad:
        raise HTTPException(400, f"Not tradable US tickers: {', '.join(bad)}")
    if len(symbols) > MAX_SYMBOLS:
        raise HTTPException(400, f"At most {MAX_SYMBOLS} symbols")
    if not body.instruction.strip():
        raise HTTPException(400, "Tell Claude what strategy to follow")

    _state.update(enabled=True, instruction=body.instruction.strip(), symbols=symbols,
                  interval_min=body.interval_min, errors=0, stopped_reason=None, next_run=None)
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
    log.info("Auto mode on: every %d min, symbols=%s", body.interval_min, symbols or "portfolio")
    return _public()


@router.post("/run")
async def run_now():
    """Run one pass immediately (also outside market hours; market orders then queue for the open)."""
    if not _state["enabled"]:
        raise HTTPException(409, "Turn auto mode on first")
    if _state["running"]:
        raise HTTPException(409, "A run is already in progress")
    try:
        await run_once()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return _public()
