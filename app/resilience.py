"""Retry, timeout and caching helpers shared by all providers."""
from __future__ import annotations

import asyncio
import functools
import logging
import random
import threading
import time
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool

from .errors import ProviderError, ProviderTimeout, RateLimited, TickerNotFound

log = logging.getLogger("dashboard.resilience")


def classify_exception(exc: Exception, ticker: str | None = None) -> ProviderError:
    """Map arbitrary vendor/network exceptions onto our error taxonomy."""
    if isinstance(exc, ProviderError):
        return exc
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(k in text for k in ("429", "too many requests", "rate limit", "ratelimit")):
        return RateLimited("Upstream rate limit hit. Please retry shortly.", ticker=ticker)
    if any(k in text for k in ("timeout", "timed out", "readtimeout", "connecttimeout")):
        return ProviderTimeout("Upstream data provider timed out.", ticker=ticker)
    if any(k in text for k in ("404", "not found", "no data found", "delisted", "no timezone")):
        return TickerNotFound(f"Ticker '{ticker}' was not found.", ticker=ticker)
    return ProviderError(f"Upstream provider error: {exc}", ticker=ticker)


async def call_with_resilience(
    fn: Callable[..., Any],
    *args: Any,
    timeout: float = 15.0,
    retries: int = 2,
    base_delay: float = 0.6,
    ticker: str | None = None,
    **kwargs: Any,
) -> Any:
    """Run a blocking provider call in a thread with a hard timeout and
    exponential-backoff retries on transient errors (timeouts / rate limits)."""
    attempt = 0
    while True:
        try:
            return await asyncio.wait_for(run_in_threadpool(fn, *args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            err: ProviderError = ProviderTimeout(
                f"Data provider did not respond within {timeout:.0f}s.", ticker=ticker
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad, we classify
            err = classify_exception(exc, ticker)
            if err is not exc:
                err.__cause__ = exc  # keep the vendor error for callers that need detail

        transient = isinstance(err, (ProviderTimeout, RateLimited))
        if not transient or attempt >= retries:
            raise err
        delay = base_delay * (2**attempt) + random.uniform(0, 0.3)
        log.warning("Transient error (%s); retry %d/%d in %.2fs", err.code, attempt + 1, retries, delay)
        await asyncio.sleep(delay)
        attempt += 1


class TTLCache:
    """Tiny thread-safe TTL cache to soften upstream rate limits."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            expires, value = hit
            if expires < time.monotonic():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)


cache = TTLCache()


def cached(ttl: float):
    """Decorator for async functions; key = function name + args."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = f"{fn.__qualname__}:{args[1:]}:{sorted(kwargs.items())}"
            hit = cache.get(key)
            if hit is not None:
                return hit
            value = await fn(*args, **kwargs)
            cache.set(key, value, ttl)
            return value

        return wrapper

    return deco
