"""Externally-pollable PROGRESS HEARTBEAT for the bounded crawl.

The daily build crawls tens of thousands of boards inside a SINGLE GitHub Actions step, and
Actions does not stream a step's logs until the step finishes -- so a 1-2h crawl is a black box
("how far along are we?" is unanswerable mid-run). This module writes a tiny, THROTTLED JSON
snapshot (boards done/total, rows so far, elapsed, the slowest-host tail) that a workflow loop
re-uploads to the release, so a watcher can poll live progress WITHOUT waiting for the step to end.

Observability ONLY. Two invariants keep the heartbeat from ever changing or slowing the crawl:

* :meth:`ProgressHeartbeat.tick` is O(1) and fully synchronous -- no ``await``, no per-board disk
  I/O. Under anyio's cooperative scheduling that makes the counter bump + throttle check atomic
  even though the pool calls it from many workers, so it needs no lock and cannot interleave.
* Every write is best-effort: an ``extra()`` callback that raises, or a writer that raises, is
  swallowed. A failed heartbeat is a lost snapshot, never a failed build.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["ProgressHeartbeat", "atomic_write_json"]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (temp file + ``os.replace``).

    The rename is atomic on POSIX, so a poller (or the workflow uploader) never observes a
    half-written file -- it sees either the previous snapshot or the new one, never a torn read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


class ProgressHeartbeat:
    """Throttled progress writer draining a per-board completion tick to a small JSON snapshot.

    Wire :meth:`tick` to :func:`ergon_tracker.crawl_pool.run_pool`'s ``on_result`` (one tick per
    completed board). It self-throttles: a snapshot is flushed only when ``interval_s`` has elapsed
    since the last flush attempt -- NOT once per board -- so the release is never spammed. The very
    first tick flushes immediately so a watcher sees progress right away.

    ``extra`` (optional) returns the run-specific fields to merge into each snapshot (e.g. rows so
    far, the slowest-host tail); it is called only when a snapshot is actually flushed, and any
    exception it raises is swallowed. ``writer`` (injectable for tests) persists the payload.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        total: int,
        *,
        interval_s: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
        extra: Callable[[], dict[str, Any]] | None = None,
        writer: Callable[[Path, dict[str, Any]], None] = atomic_write_json,
    ) -> None:
        self._path = Path(path)
        self._total = int(total)
        self._interval = float(interval_s)
        self._clock = clock
        self._extra = extra
        self._writer = writer
        self._done = 0
        self._start = clock()
        self._last_emit: float | None = None
        self.writes = 0  # successful snapshot writes -- observability + test assertions

    def tick(self, n: int = 1) -> None:
        """Record ``n`` completed board(s); flush a snapshot at most once per ``interval_s``.

        Synchronous and O(1): the counter bump is atomic under cooperative scheduling, and the
        throttle is advanced by ATTEMPT (not by success), so even a persistently-failing writer can
        never turn this into per-board I/O. Never raises.
        """
        self._done += n
        now = self._clock()
        if self._last_emit is None or (now - self._last_emit) >= self._interval:
            # Advance the throttle first: a write that raises still counts as an attempt, so a bad
            # disk never causes a per-board retry storm (worst case: one lost snapshot this window).
            self._last_emit = now
            self._flush(now)

    def emit(self) -> None:
        """Force a snapshot NOW, bypassing the throttle (seed at start, finalize at end)."""
        now = self._clock()
        self._last_emit = now
        self._flush(now)

    def _flush(self, now: float) -> None:
        payload: dict[str, Any] = {
            "boards_done": self._done,
            "boards_total": self._total,
            "elapsed_s": round(now - self._start, 1),
        }
        if self._extra is not None:
            # extra() (host accounting etc.) must never break the crawl -> swallow anything it raises.
            with contextlib.suppress(Exception):
                payload.update(self._extra())
        try:
            self._writer(self._path, payload)
        except Exception:  # noqa: BLE001 -- a failed heartbeat write is non-fatal, by design
            return
        self.writes += 1
