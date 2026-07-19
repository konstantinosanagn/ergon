"""Daily freshness sweep: id-only board-MEMBERSHIP check (not liveness, not JD recovery).

WHY: today's only re-verification paths are the (slow, expensive) tiered rebuild and
``index/liveness.py``'s dead-apply-link pass -- neither re-checks "is this posting still LISTED
on its board" on any tight cadence (liveness's own effective interval is weeks; the tiered build's
window gate is ~5-23 days, see the design doc). A posting a board has silently dropped (closed,
filled, pulled) can stay ``status='active'`` in our index for a long time. This module closes that
gap CHEAPLY: check **boards, not postings** (58k boards vs 1.48M postings) and fetch **id-only**
(skip JD/enrich/dedup/insert -- the costly parts a board-membership check never needs).

A departed posting = an id our index has stored as this board's ``status='active'`` set, that is
absent from the board's CURRENT id set. Action: ``UPDATE jobs SET status='expired', ...`` --
already filtered by every query path (``index/query.py``'s ``j.status = 'active'`` gate), so this
is invisible immediately with zero query-layer changes, and ``COUNT(*)`` is unaffected (never a
hard delete), so the row_floor publish gate (``gates.py``) can never trip on it.

PHASE 0 SCOPE (see docs/superpowers/specs/2026-07-18-daily-freshness-sweep-design.md): only the
``DETERMINISTIC_SOURCES`` -- boards whose list response is a full, un-paginated dump, so a missing
id is a REAL departure with no false-positive risk. Search-index-style sources (oracle,
smartrecruiters, successfactors, ...) reshuffle/paginate non-deterministically (mirrors
``liveness.py``'s ``CONFIRM_VIA_DETAIL_SOURCES`` finding) -- a list-miss there needs a per-posting
404 confirmation before it's safe to expire on, which is a LATER phase. Passing a search-index
source into ``sweep_boards`` is a no-op by construction (excluded via ``deterministic_sources``),
never a silent false-expire.

Unlike ``liveness.py`` (whose ``fetch_board``/``fetch_detail`` are injected callables so the
module never imports the network stack), this module calls ``get_provider(source).fetch(...)``
directly -- the design's own instruction: id-only board membership is just an ordinary provider
fetch with only the id column kept, no new per-provider ``list_ids`` endpoint needed for Phase 0.
Tests stay fully offline by monkeypatching ``get_provider`` (see ``providers/base.py``), exactly
like the existing crawl tests (``test_crawl_conditional.py``) already do.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import anyio

from ..models import SearchQuery
from ..providers.base import get_provider

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = [
    "DETERMINISTIC_SOURCES",
    "board_live_ids",
    "departed_ids",
    "sweep_boards",
]

# Sources whose provider ``fetch()`` returns the board's FULL, un-paginated, deterministic dump --
# a missing stored id is a genuine departure, safe to expire on a SINGLE miss (no streak, no
# per-posting confirm needed). Search-index-style sources (oracle, smartrecruiters,
# successfactors, icims, eightfold, ...) are deliberately excluded here -- their list APIs
# reshuffle/paginate (measured 50-100% list-miss false-positive rates on the analogous liveness
# pass, see ``liveness.py``'s ``CONFIRM_VIA_DETAIL_SOURCES``) and need a per-posting-confirm path
# that is a LATER phase of this sweep, not Phase 0.
DETERMINISTIC_SOURCES: frozenset[str] = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "breezy",
        "workable",
        "jazzhr",
        "rippling",
        "join",
        "dejobs",
    }
)

# In-flight board fetches (env-tunable, mirrors liveness.py's ERGON_LIVENESS_CONCURRENCY /
# detail.py's ERGON_DETAIL_CONCURRENCY). Politeness is enforced BELOW this by the caller-supplied
# AsyncFetcher's own per-host token-bucket -- this only bounds how many boards' worth of id-only
# fetches run concurrently at once.
_FRESHNESS_CONCURRENCY = int(os.environ.get("ERGON_FRESHNESS_CONCURRENCY", "32"))

# Chunk size for parameterized ``id IN (...)`` UPDATEs -- stays well under SQLite's bound-variable
# ceiling for a board with an unusually large departed set (mirrors detail.py's
# ``_requeue_for_location_backfill`` chunking).
_UPDATE_CHUNK = 500


async def board_live_ids(source: str, token: str, fetcher: AsyncFetcher) -> set[str] | None:
    """The board's CURRENT posting id-set, id-only -- or ``None`` on any fetch error (never
    raises). A ``None`` means "could not determine" and must NOT be treated as "board is empty":
    the caller (``sweep_boards``) is responsible for never expiring anything off a ``None``.

    Reuses the provider's ordinary ``fetch()`` (the id-only-ness is just "keep only the id
    column" -- no new per-provider endpoint for Phase 0) rather than any JD/enrich/dedup/insert
    machinery, so this is cheap relative to a real crawl of the same board.
    """
    prov = get_provider(source)
    if prov is None:
        return None
    try:
        raws = await prov.fetch(token, SearchQuery(), fetcher)
    except Exception:  # noqa: BLE001 - a dead/blocked/erroring board -> "could not determine"
        return None
    try:
        return {str(r.source_job_id) for r in raws}
    except Exception:  # noqa: BLE001 - a malformed raw must not fail the whole board
        return None


def departed_ids(stored_active_ids: set[str], live_ids: set[str] | None) -> set[str]:
    """Pure diff: the stored ``active`` ids that are NOT in the board's current live set.

    ``live_ids is None`` (an errored/undetermined fetch) ALWAYS returns an empty set -- a
    transient board-fetch failure must never be mistaken for "every stored posting departed".
    """
    if live_ids is None:
        return set()
    return stored_active_ids - live_ids


def _stored_active_by_board(
    con: sqlite3.Connection, sources: Iterable[str]
) -> dict[tuple[str, str], dict[str, str]]:
    """Every ``status='active'`` job's (source, board_token) -> {source_job_id: job.id}, scoped to
    ``sources`` and restricted to rows with a resolvable board_token, in ONE query (not one query
    per board) -- joins ``job_sources`` (the only table carrying the raw, provider-native
    ``source_job_id``; ``jobs`` itself only stores the derived, hashed ``id``) back onto ``jobs``
    on the matching source, so a board's stored active id-set can be diffed directly against
    ``board_live_ids``'s raw-id-space result.
    """
    sources = list(sources)
    if not sources:
        return {}
    placeholders = ",".join("?" for _ in sources)
    prev_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT j.source AS source, j.board_token AS board_token, j.id AS job_id, "
            "js.source_job_id AS source_job_id "
            "FROM jobs j JOIN job_sources js ON js.job_id = j.id AND js.source = j.source "
            "WHERE j.status = 'active' AND j.board_token IS NOT NULL "
            f"AND j.source IN ({placeholders})",  # noqa: S608 - placeholders, not values
            sources,
        ).fetchall()
    finally:
        con.row_factory = prev_factory

    out: dict[tuple[str, str], dict[str, str]] = {}
    for r in rows:
        key = (str(r["source"]), str(r["board_token"]))
        out.setdefault(key, {})[str(r["source_job_id"])] = str(r["job_id"])
    return out


def _expire_job_ids(con: sqlite3.Connection, job_ids: list[str], expired_at: str) -> None:
    """Flip the given ``jobs.id`` rows to ``status='expired'`` IN PLACE -- never hard-deleted, so
    ``COUNT(*) FROM jobs`` is unaffected and the row_floor publish gate can never trip on this
    pass. Chunked to stay under SQLite's bound-variable ceiling for an unusually large batch."""
    for start in range(0, len(job_ids), _UPDATE_CHUNK):
        batch = job_ids[start : start + _UPDATE_CHUNK]
        placeholders = ",".join("?" for _ in batch)
        con.execute(
            "UPDATE jobs SET status = 'expired', expired_at = ?, "
            f"expiry_reason = 'departed_board' WHERE id IN ({placeholders})",  # noqa: S608
            [expired_at, *batch],
        )


async def sweep_boards(
    boards: Iterable[tuple[str, str]],
    con: sqlite3.Connection,
    fetcher: AsyncFetcher,
    *,
    deterministic_sources: frozenset[str] | set[str] = DETERMINISTIC_SOURCES,
    concurrency: int = _FRESHNESS_CONCURRENCY,
    now: Callable[[], str],
) -> dict[str, dict[str, int]]:
    """The Phase-0 sweep: for each ``(source, token)`` in ``boards`` whose ``source`` is in
    ``deterministic_sources``, fetch the board's current live id-set, diff it against this
    board's stored ``status='active'`` ids (one query for the whole batch, see
    ``_stored_active_by_board``), and flip any departed row to ``status='expired'``.

    Boards on a non-deterministic source are filtered out up front -- never fetched, never
    touched -- so passing the full board list (including search-index sources, a later phase) is
    always safe by construction.

    Concurrency is bounded by ``concurrency`` (env ``ERGON_FRESHNESS_CONCURRENCY``, default 32) via
    an ``anyio.CapacityLimiter``, mirroring ``liveness.py``/``detail.py``'s pattern -- politeness
    itself is enforced BELOW this by ``fetcher``'s own per-host token-bucket. Every sqlite write
    (the ``status='expired'`` UPDATE) is serialized through a single lock, since board fetches run
    concurrently but this module makes no assumption about ``con``'s thread-safety beyond that.

    ``now`` is injected (no wall-clock read here) -- one timestamp is used for every expiry this
    call makes, matching ``liveness.py``'s convention.

    Returns per-source counts: ``{source: {"checked", "departed", "expired", "errored"}}``.
    ``checked`` counts boards attempted (including errored ones); ``departed`` counts ids found
    absent from a board's live set; ``expired`` counts rows actually flipped (equal to
    ``departed`` in Phase 0 -- every departed id on a deterministic board is expired immediately,
    no streak/confirm gate); ``errored`` counts boards whose fetch could not be determined.
    """
    target_boards = [(s, t) for s, t in boards if s in deterministic_sources]
    if not target_boards:
        return {}

    sources_in_play = sorted({s for s, _ in target_boards})
    stored = _stored_active_by_board(con, sources_in_play)

    counts: dict[str, dict[str, int]] = {
        s: {"checked": 0, "departed": 0, "expired": 0, "errored": 0} for s in sources_in_play
    }

    write_lock = anyio.Lock()
    limiter = anyio.CapacityLimiter(max(1, concurrency))
    now_s = now()

    async def process(source: str, token: str) -> None:
        async with limiter:
            live_ids = await board_live_ids(source, token, fetcher)
        board_map = stored.get((source, token), {})
        stored_ids = set(board_map.keys())
        missing = departed_ids(stored_ids, live_ids)
        async with write_lock:
            counts[source]["checked"] += 1
            if live_ids is None:
                counts[source]["errored"] += 1
                return
            if not missing:
                return
            counts[source]["departed"] += len(missing)
            job_ids = [board_map[sid] for sid in missing]
            _expire_job_ids(con, job_ids, now_s)
            counts[source]["expired"] += len(job_ids)

    async with anyio.create_task_group() as tg:
        for source, token in target_boards:
            tg.start_soon(process, source, token)

    con.commit()
    return counts
