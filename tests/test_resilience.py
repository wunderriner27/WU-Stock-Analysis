import asyncio
import time

import pytest

from app import resilience
from app.errors import ProviderError, ProviderTimeout, RateLimited, TickerNotFound
from app.resilience import TTLCache, cached, call_with_resilience, classify_exception


@pytest.mark.parametrize("exc, kind", [
    (Exception("HTTP 429 Too Many Requests"), RateLimited),
    (Exception("Read timed out"), ProviderTimeout),
    (Exception("$ZZZZ: possibly delisted; no timezone found"), TickerNotFound),
    (ConnectionError("CONNECT tunnel failed, response 403"), ProviderError),
])
def test_classify_exception(exc, kind):
    err = classify_exception(exc, "X")
    assert type(err) is kind
    assert err.ticker == "X"


def test_classify_passes_provider_errors_through():
    e = TickerNotFound("nope")
    assert classify_exception(e) is e


def run(coro):
    return asyncio.run(coro)


def test_retries_transient_errors_then_succeeds(monkeypatch):
    monkeypatch.setattr(resilience.asyncio, "sleep", _no_sleep)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Exception("429 rate limit")
        return "ok"

    assert run(call_with_resilience(flaky, retries=2)) == "ok"
    assert len(calls) == 3


def test_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(resilience.asyncio, "sleep", _no_sleep)
    calls = []

    def always_limited():
        calls.append(1)
        raise Exception("Too Many Requests")

    with pytest.raises(RateLimited):
        run(call_with_resilience(always_limited, retries=2))
    assert len(calls) == 3


def test_does_not_retry_permanent_errors():
    calls = []

    def missing():
        calls.append(1)
        raise Exception("404 Not Found")

    with pytest.raises(TickerNotFound) as ei:
        run(call_with_resilience(missing, ticker="ZZZ"))
    assert len(calls) == 1
    assert ei.value.__cause__ is not None


def test_timeout_becomes_provider_timeout():
    with pytest.raises(ProviderTimeout):
        run(call_with_resilience(time.sleep, 0.5, timeout=0.05, retries=0))


async def _no_sleep(_):
    return None


def test_ttl_cache_expires(monkeypatch):
    c = TTLCache()
    now = [1000.0]
    monkeypatch.setattr(resilience.time, "monotonic", lambda: now[0])
    c.set("k", "v", ttl=10)
    assert c.get("k") == "v"
    now[0] += 11
    assert c.get("k") is None


def test_cached_decorator_keys_on_args():
    calls = []

    class Svc:
        @cached(ttl=60)
        async def get(self, sym):
            calls.append(sym)
            return sym.lower()

    s = Svc()
    assert run(s.get("AAPL")) == "aapl"
    assert run(s.get("AAPL")) == "aapl"
    assert run(s.get("MSFT")) == "msft"
    assert calls == ["AAPL", "MSFT"]
