"""Exception hierarchy for ergon (FROZEN CONTRACT)."""

from __future__ import annotations

__all__ = [
    "ErgonError",
    "ProviderError",
    "FetchError",
    "TransientHTTPError",
    "RateLimitError",
    "ResolveError",
]


class ErgonError(Exception):
    """Base class for all ergon errors."""


class ProviderError(ErgonError):
    """A provider failed to fetch or normalize. Carries the provider name."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class FetchError(ErgonError):
    """A network/HTTP fetch failed in a non-retryable way (or after exhausting retries)."""


class CircuitOpenError(FetchError):
    """The per-host circuit breaker refused the call; the host is cooling down.

    Distinct from a generic FetchError because it means something different to a caller with a
    fallback lane: a 403 says "this transport is blocked, try a heavier one", while THIS says
    "stop touching this host at all". Providers with an escalation ladder (schemaorg, apicapture)
    must re-raise it rather than escalate — otherwise the breaker firing is what CAUSES the
    bypass, and the harder the host pushes back the harder we hit it.
    """


class TransientHTTPError(FetchError):
    """A retryable server-side HTTP error (5xx). Used to drive the retry loop."""


class RateLimitError(FetchError):
    """HTTP 429. Carries ``retry_after`` seconds when the server provided it."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class ResolveError(ErgonError):
    """ATS auto-discovery could not determine a provider/token for a URL or domain."""
