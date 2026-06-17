"""Minimal sqlite-backed state for poll-and-diff (the seam for a future streaming layer).

Stores which job ids have been seen per source so callers can ask "what's new since last
run?". Intentionally small in v1 — it exists so the streaming/scheduler platform can grow on
top without reworking the core.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import JobPosting

__all__ = ["JobStore"]

_DEFAULT_PATH = Path(".ergon_tracker/state.sqlite")


class JobStore:
    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_jobs ("
            " job_id TEXT NOT NULL,"
            " source TEXT NOT NULL,"
            " first_seen TEXT NOT NULL,"
            " PRIMARY KEY (job_id, source)"
            ")"
        )
        self._conn.commit()

    def new_ids(self, source: str, job_ids: Iterable[str]) -> set[str]:
        """Return the subset of ``job_ids`` not previously recorded for ``source``."""
        ids = set(job_ids)
        if not ids:
            return set()
        rows = self._conn.execute(
            "SELECT job_id FROM seen_jobs WHERE source = ?", (source,)
        ).fetchall()
        seen = {r[0] for r in rows}
        return ids - seen

    def mark_seen(self, jobs: Iterable[JobPosting]) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen_jobs (job_id, source, first_seen) VALUES (?, ?, ?)",
            [(j.id, j.source, now) for j in jobs],
        )
        self._conn.commit()

    def diff(self, jobs: list[JobPosting]) -> list[JobPosting]:
        """Return only the jobs not seen before, and record all of them as seen."""
        by_source: dict[str, list[JobPosting]] = {}
        for j in jobs:
            by_source.setdefault(j.source, []).append(j)
        fresh: list[JobPosting] = []
        for source, group in by_source.items():
            new = self.new_ids(source, (j.id for j in group))
            fresh.extend(j for j in group if j.id in new)
        self.mark_seen(jobs)
        return fresh

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> JobStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
