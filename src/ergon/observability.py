"""Observability helpers — make partial failures and silent drops *visible*.

The number-one reliability complaint about scraper-based job libraries (e.g. JobSpy) is the
"silent failure": a source quietly returns zero rows and the caller never knows. These helpers
let the orchestrator attach per-source health, time each source, and flag suspicious count
drops without inventing any control flow of its own.
"""

from __future__ import annotations

import time
from types import TracebackType

from .models import SourceHealth

__all__ = [
    "build_health",
    "Timer",
    "time_source",
    "count_sanity_check",
    "summarize",
]


def build_health(
    source: str,
    *,
    ok: bool,
    count: int = 0,
    error: str | None = None,
    elapsed_ms: int = 0,
    truncated: bool = False,
    as_of: str | None = None,
) -> SourceHealth:
    """Thin, explicit constructor for a per-source health record."""
    return SourceHealth(
        source=source,
        ok=ok,
        count=count,
        error=error,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
        as_of=as_of,
    )


class Timer:
    """Sync context manager that measures wall-clock elapsed time in milliseconds.

    Usage::

        with time_source() as t:
            ...
        health = build_health("greenhouse", ok=True, count=n, elapsed_ms=t.elapsed_ms)

    ``elapsed_ms`` is readable while still inside the block (live reading) and frozen after
    exit.
    """

    __slots__ = ("_start", "_end")

    def __init__(self) -> None:
        self._start: float = time.perf_counter()
        self._end: float | None = None

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        self._end = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._end = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        end = self._end if self._end is not None else time.perf_counter()
        return int((end - self._start) * 1000)


def time_source() -> Timer:
    """Return a fresh :class:`Timer` to use as a context manager around a source fetch."""
    return Timer()


def count_sanity_check(
    source: str,
    count: int,
    *,
    baseline: int | None = None,
    drop_ratio: float = 0.5,
) -> str | None:
    """Warn when a source returns far fewer jobs than its known baseline.

    Returns a human-readable warning string when ``baseline`` is provided and ``count`` has
    dropped below ``baseline * drop_ratio`` (the JobSpy "silent failure" guard); otherwise
    ``None``.
    """
    if baseline is None or baseline <= 0:
        return None
    floor = baseline * drop_ratio
    if count < floor:
        return (
            f"{source}: count dropped to {count} (baseline {baseline}, "
            f"expected >= {floor:.0f}) — possible silent failure"
        )
    return None


def summarize(healths: list[SourceHealth]) -> dict[str, object]:
    """Aggregate per-source health into headline totals.

    Keys: ``ok_count``, ``failed_count``, ``total_jobs``, ``failed`` (list of failed source
    names).
    """
    ok = [h for h in healths if h.ok]
    failed = [h for h in healths if not h.ok]
    return {
        "ok_count": len(ok),
        "failed_count": len(failed),
        "total_jobs": sum(h.count for h in healths),
        "failed": [h.source for h in failed],
    }
