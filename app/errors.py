"""Provider-agnostic error types. Every provider maps its own failures onto these,
so the API layer never needs to know which vendor is underneath."""


class ProviderError(Exception):
    status_code = 502
    code = "provider_error"

    def __init__(self, message: str, *, ticker: str | None = None):
        super().__init__(message)
        self.message = message
        self.ticker = ticker


class TickerNotFound(ProviderError):
    status_code = 404
    code = "ticker_not_found"


class ProviderTimeout(ProviderError):
    status_code = 504
    code = "provider_timeout"


class RateLimited(ProviderError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int = 30, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after
