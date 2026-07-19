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

PHASE 0 SCOPE: only the ``DETERMINISTIC_SOURCES`` -- boards whose list response is a full,
un-paginated dump, so a missing id is a REAL departure with no false-positive risk.
``sweep_boards`` filters everything else out up front (never fetched, never touched), so passing
the full board list -- including PHASE 1's search-index sources -- was always safe by
construction.

PHASE 1 SCOPE (see docs/superpowers/specs/2026-07-18-daily-freshness-sweep-design.md):
``SEARCH_INDEX_SOURCES`` -- ``{oracle, smartrecruiters, successfactors, icims, eightfold}`` --
whose list APIs reshuffle/paginate non-deterministically (mirrors ``liveness.py``'s
``CONFIRM_VIA_DETAIL_SOURCES`` finding, measured 50-100% list-miss false-positive rates). A
board-membership miss on one of these is only a CANDIDATE, never a departure by itself; it must be
CONFIRMED via the provider's per-posting ``fetch_detail`` (already wired for Tier-3 JD recovery,
see ``index/detail.py``'s ``DetailRef``/``reconcile_detail_tier``) before anything is expired.
``fetch_detail``'s return/raise contract: a returned ``None``/empty means the posting is gone
(a definitive 404/410 -- confirmed dead); a real ``DetailFetch``/``str`` means it's still live (the
list-miss was a false positive -- KEEP the row active); a RAISED exception is "could not determine"
(transient/indeterminate) -- also KEEP, retry next run. Two routing strategies
(``sweep_search_index_boards``), per the design's measured per-provider strategy table:

- ``oracle``/``smartrecruiters``/``successfactors`` (bulk list is cheap-ish): bulk-relist for the
  board's current id set, diff against stored (``departed_ids``, reused byte-identically), and
  confirm ONLY the resulting candidate delta via ``fetch_detail`` -- bounded by construction (the
  delta is small relative to the board).
- ``icims``/``eightfold`` (bulk list is pathologically bloated -- icims ~33KB/job, eightfold ~97%
  facet redundancy, see the design doc): skip the bulk relist for membership entirely and
  per-posting-confirm directly against the board's stored active ids, bounded by
  ``ERGON_FRESHNESS_SEARCH_INDEX_BOARD_LIMIT`` (default 200) per board per run, on top of the
  shared concurrency cap.

``sweep_all_boards`` composes both phases (Phase-0 deterministic + Phase-1 search-index) over one
board list in a single call -- the two source sets are disjoint by construction, so their stats
dicts never collide.

Unlike ``liveness.py`` (whose ``fetch_board``/``fetch_detail`` are injected callables so the
module never imports the network stack), this module calls ``get_provider(source).fetch(...)``
and ``get_provider(source).fetch_detail(...)`` directly -- the design's own instruction: id-only
board membership and per-posting confirmation are just ordinary provider calls, no new
per-provider endpoint needed. Tests stay fully offline by monkeypatching ``get_provider`` (see
``providers/base.py``), exactly like the existing crawl tests (``test_crawl_conditional.py``)
already do.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import anyio

from ..models import DetailFetch, SearchQuery
from ..providers.base import get_provider
from .detail import DetailRef

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = [
    "DETERMINISTIC_SOURCES",
    "SEARCH_INDEX_SOURCES",
    "board_live_ids",
    "confirm_departed",
    "departed_ids",
    "sweep_all_boards",
    "sweep_boards",
    "sweep_search_index_boards",
]

# Sources whose provider ``fetch()`` returns the board's FULL, un-paginated, deterministic dump --
# a missing stored id is a genuine departure, safe to expire on a SINGLE miss (no streak, no
# per-posting confirm needed). ``SEARCH_INDEX_SOURCES`` (below) are deliberately excluded here --
# their list APIs reshuffle/paginate (measured 50-100% list-miss false-positive rates on the
# analogous liveness pass, see ``liveness.py``'s ``CONFIRM_VIA_DETAIL_SOURCES``) and are handled by
# ``sweep_search_index_boards`` instead, never by this deterministic single-miss path.
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
        # Phase 2 additions -- each live-verified (2026-07-19 recon) to return the COMPLETE board
        # from fetch(): either a single unpaginated GET (recruitee, teamtailor, personio, bamboohr,
        # jobvite, applicantpro) or paginated to the server's own authoritative total-count that the
        # fetched id-set matched exactly (jobdiva, brassring, ripplehire). Sources whose fetch()
        # caps/reshuffles (apicapture, taleo, themuse, pinpoint, avature, taleobe) are deliberately
        # NOT here -- an incomplete list on this single-miss path would mass-expire live postings.
        "recruitee",
        "teamtailor",
        "personio",
        "bamboohr",
        "brassring",
        "jobdiva",
        "jobvite",
        "applicantpro",
        "ripplehire",
    }
)

# PHASE 1: search-index-style sources whose board list reshuffles/paginates non-deterministically
# -- a board-membership miss here is only a CANDIDATE, never a confirmed departure (see module
# docstring). Split into the two measured routing strategies:
#
# - bulk-relist-then-confirm: the board's bulk list is still cheap enough to re-fetch for
#   membership; only the resulting missing-delta gets a per-posting ``fetch_detail`` confirm.
_BULK_RELIST_CONFIRM_SOURCES: frozenset[str] = frozenset(
    {
        "oracle",
        "smartrecruiters",
        "successfactors",
        # Phase 2 additions -- each has a fetch() usable as a cheap candidate relist AND a
        # live-verified real ``fetch_detail`` (2026-07-19 recon) that returns None on a genuine
        # HTTP 404 (workday's cxs detail, radancy's apply-page GET, ukg's OpportunityDetail), so a
        # list-miss candidate is only expired after a per-posting confirm. workday MUST be here (not
        # deterministic): its list caps at 2000 with a lossy single-level facet fallback, so a bare
        # list-miss is untrustworthy -- but the 404-confirm makes it safe, and this brings workday's
        # ~37% index share into coverage.
        "workday",
        "radancy",
        "ukg",
    }
)
# - per-posting-only: the bulk list is pathologically bloated (icims ~33KB/job; eightfold ~97%
#   facet redundancy) -- skip it for membership entirely and confirm directly against the stored
#   active ids (bounded by ``_SEARCH_INDEX_BOARD_LIMIT``).
_PER_POSTING_CONFIRM_SOURCES: frozenset[str] = frozenset({"icims", "eightfold"})

SEARCH_INDEX_SOURCES: frozenset[str] = _BULK_RELIST_CONFIRM_SOURCES | _PER_POSTING_CONFIRM_SOURCES

# In-flight board fetches (env-tunable, mirrors liveness.py's ERGON_LIVENESS_CONCURRENCY /
# detail.py's ERGON_DETAIL_CONCURRENCY). Politeness is enforced BELOW this by the caller-supplied
# AsyncFetcher's own per-host token-bucket -- this only bounds how many boards' worth of id-only
# fetches (and, in Phase 1, per-posting confirm fetches) run concurrently at once. Shared by both
# ``sweep_boards`` and ``sweep_search_index_boards`` so a combined ``sweep_all_boards`` run never
# exceeds this cap in aggregate host-fetch pressure by construction (one limiter instance per call,
# not per source).
_FRESHNESS_CONCURRENCY = int(os.environ.get("ERGON_FRESHNESS_CONCURRENCY", "32"))

# Defensive cap on the deterministic single-miss path. A partial-but-NON-empty live fetch (a
# provider that paginates and swallows a mid-crawl page error, returning only the first pages)
# would make the un-fetched tail look "departed" -- the empty-set valve above only catches a FULLY
# empty result. Real boards rarely shed most of their postings between daily sweeps, so never
# expire more than this fraction of a board's stored-active set in a single deterministic pass;
# above it, treat the board as undetermined (like the empty-set valve). Applied only to boards with
# at least ``_MIN_BOARD_FOR_FRACTION_GUARD`` stored ids -- small boards legitimately churn hard in
# percentage terms (e.g. 3 of 4 reqs closing), so the guard would false-trigger on them.
_MAX_BOARD_EXPIRE_FRACTION = float(os.environ.get("ERGON_FRESHNESS_MAX_EXPIRE_FRACTION", "0.5"))
_MIN_BOARD_FOR_FRACTION_GUARD = int(os.environ.get("ERGON_FRESHNESS_FRACTION_GUARD_MIN", "20"))

# Per-board cap on how many stored active ids ``sweep_search_index_boards`` will per-posting-confirm
# in one run for a ``_PER_POSTING_CONFIRM_SOURCES`` board (icims/eightfold have no cheap bulk-list
# membership signal, so without this a single huge board could dominate a whole run). A board with
# more stored active ids than the limit simply isn't fully re-verified in one pass -- the rest are
# picked up on a later run (no cursor/rotation needed for Phase 1; a future phase could add one).
_SEARCH_INDEX_BOARD_LIMIT = int(os.environ.get("ERGON_FRESHNESS_SEARCH_INDEX_BOARD_LIMIT", "200"))

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
        # Safety valve: an EMPTY live id-set while we still hold active postings for this board is
        # indistinguishable from a silently-swallowed fetch failure -- some providers (e.g. jazzhr,
        # dejobs) return ``[]`` on a transient 429/5xx/timeout instead of raising, so
        # ``board_live_ids`` yields ``set()`` rather than ``None``, and a naive diff would expire
        # the WHOLE board's live postings. Never expire 100% of a board off a single empty result:
        # treat it as "undetermined" (like ``None``). A genuine full-closure is still caught by
        # the query-time last_seen staleness filter and the tiered crawl/liveness pass.
        undetermined = live_ids is None or (not live_ids and bool(stored_ids))
        missing = set() if undetermined else departed_ids(stored_ids, live_ids)
        # Partial-fetch guard (see _MAX_BOARD_EXPIRE_FRACTION): too-large a single-pass departure
        # fraction on a sizeable board looks like a truncated/partial fetch (a paginated provider
        # that swallowed a mid-crawl page error), not genuine churn -- treat as undetermined rather
        # than expire the un-fetched tail.
        if (
            not undetermined
            and len(stored_ids) >= _MIN_BOARD_FOR_FRACTION_GUARD
            and len(missing) > _MAX_BOARD_EXPIRE_FRACTION * len(stored_ids)
        ):
            undetermined = True
            missing = set()
        async with write_lock:
            counts[source]["checked"] += 1
            if undetermined:
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


# --- Phase 1: search-index sources (candidate + per-posting confirm) --------------------------

_SEARCH_INDEX_REF_COLUMNS = (
    "j.id AS id, j.source AS source, j.board_token AS board_token, j.apply_url AS apply_url, "
    "j.listing_url AS listing_url, j.content_hash AS content_hash, j.title AS title, "
    "j.level AS level, js.source_job_id AS source_job_id"
)


def _stored_active_rows_by_board(
    con: sqlite3.Connection, sources: Iterable[str]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every ``status='active'`` row's (source, board_token) -> [row, ...], carrying every column
    ``DetailRef.from_row`` needs (id, source, board_token, apply_url, listing_url, content_hash,
    title, level) PLUS the raw ``source_job_id`` (from ``job_sources``, mirroring
    ``_stored_active_by_board``'s join) -- so a search-index board's candidate delta can be both
    diffed (in ``source_job_id`` space) and, for each candidate, turned directly into a
    ``DetailRef`` for the per-posting confirm fetch without a second query.
    """
    sources = list(sources)
    if not sources:
        return {}
    placeholders = ",".join("?" for _ in sources)
    prev_factory = con.row_factory
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT {_SEARCH_INDEX_REF_COLUMNS} "  # noqa: S608 - placeholders, not values
            "FROM jobs j JOIN job_sources js ON js.job_id = j.id AND js.source = j.source "
            "WHERE j.status = 'active' AND j.board_token IS NOT NULL "
            f"AND j.source IN ({placeholders})",
            sources,
        ).fetchall()
    finally:
        con.row_factory = prev_factory

    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (str(r["source"]), str(r["board_token"]))
        out.setdefault(key, []).append(dict(r))
    return out


async def confirm_departed(ref: DetailRef, fetcher: AsyncFetcher) -> bool | None:
    """Per-posting confirmation for a search-index board-membership CANDIDATE (a stored id absent
    from a reshuffled/paginated board list -- not yet known to be a real departure).

    Returns:
      - ``True``  -- CONFIRMED DEAD: ``fetch_detail`` returned ``None``/empty text, which under its
        return/raise contract (``providers/base.py``) means a DEFINITIVE not-found (an explicit HTTP
        404/410 or a verified soft-404 body), the only signal that expires a row.
      - ``False`` -- CONFIRMED ALIVE: a real detail (non-empty text) came back -- the board-list
        miss was a reshuffle/pagination false positive, not a departure. The caller MUST keep this
        row active.
      - ``None``  -- COULD NOT DETERMINE: unknown provider, or ``fetch_detail`` RAISED -- its
        contract for an INDETERMINATE/TRANSIENT failure (5xx/429/timeout/parse error/unbuildable
        detail URL). The caller MUST keep this row active and let a later run retry -- an error (as
        opposed to a definitive 404) is never evidence of death.
    """
    prov = get_provider(ref.source)
    if prov is None:
        return None
    try:
        result = await prov.fetch_detail(ref, fetcher)
    except Exception:  # noqa: BLE001 - defend past the documented non-raising contract too
        return None
    if isinstance(result, DetailFetch):
        return not bool(result.text)
    return not bool(result)


def _new_search_index_counts(sources: Iterable[str]) -> dict[str, dict[str, int]]:
    return {
        s: {
            "checked": 0,
            "candidates": 0,
            "expired": 0,
            "confirmed_alive": 0,
            "unconfirmed": 0,
            "errored": 0,
        }
        for s in sources
    }


async def sweep_search_index_boards(
    boards: Iterable[tuple[str, str]],
    con: sqlite3.Connection,
    fetcher: AsyncFetcher,
    *,
    search_index_sources: frozenset[str] | set[str] = SEARCH_INDEX_SOURCES,
    bulk_relist_confirm_sources: frozenset[str] | set[str] = _BULK_RELIST_CONFIRM_SOURCES,
    per_posting_confirm_sources: frozenset[str] | set[str] = _PER_POSTING_CONFIRM_SOURCES,
    concurrency: int = _FRESHNESS_CONCURRENCY,
    board_active_id_limit: int = _SEARCH_INDEX_BOARD_LIMIT,
    now: Callable[[], str],
) -> dict[str, dict[str, int]]:
    """The Phase-1 sweep: for each ``(source, token)`` in ``boards`` whose ``source`` is in
    ``search_index_sources``, find CANDIDATE departures and confirm each one via the provider's
    per-posting ``fetch_detail`` before ever expiring it -- a bare board-list miss is NEVER trusted
    on its own here (unlike ``sweep_boards``'s deterministic single-miss path), because these
    sources' list APIs reshuffle/paginate (measured 50-100% list-miss false-positive rate).

    Two routing strategies (see module docstring):

    - ``bulk_relist_confirm_sources`` (oracle/smartrecruiters/successfactors): re-fetch the
      board's current id set (``board_live_ids``, the same Phase-0 primitive), diff it against
      the stored active ids (``departed_ids``, reused byte-identically) to get the candidate
      delta, then confirm ONLY that delta.
    - ``per_posting_confirm_sources`` (icims/eightfold): skip the bulk relist for membership
      entirely -- every stored active id on the board (bounded by ``board_active_id_limit``,
      default 200, env ``ERGON_FRESHNESS_SEARCH_INDEX_BOARD_LIMIT``) is a candidate, confirmed
      directly.

    A source in ``search_index_sources`` that is in neither sub-set is skipped for that board
    (still counted in ``checked``-adjacent bookkeeping would require it to be in one of the two
    sub-sets by construction of the defaults; callers overriding the sub-sets are responsible for
    keeping every ``search_index_sources`` member routable).

    Boards on a source outside ``search_index_sources`` are filtered out up front -- never
    touched by this function -- so passing the full board list (including Phase-0 deterministic
    sources) is always safe by construction; ``sweep_all_boards`` relies on exactly this to
    compose both phases over one board list.

    CONCURRENCY: a single shared ``anyio.CapacityLimiter`` (``concurrency``, default 32, env
    ``ERGON_FRESHNESS_CONCURRENCY`` -- the SAME cap ``sweep_boards`` uses) bounds every network
    call this function makes, board-list fetches AND per-posting confirm fetches alike, so the
    aggregate in-flight request count never exceeds it regardless of how many candidates a board
    produces. Politeness itself is enforced BELOW this by ``fetcher``'s own per-host token bucket.
    Every sqlite write is serialized through a single lock.

    SAFETY: a candidate is expired if and only if ``confirm_departed`` returns ``True``. A
    ``False`` (confirmed alive) or ``None`` (undetermined/errored) NEVER expires anything --
    ``unconfirmed`` candidates are left ``active`` for a later run to retry.

    Returns per-source counts:
    ``{source: {"checked", "candidates", "expired", "confirmed_alive", "unconfirmed", "errored"}}``.
    ``checked`` counts boards attempted; ``candidates`` counts ids that needed a confirm fetch;
    ``expired`` counts confirmed-dead rows actually flipped; ``confirmed_alive`` counts candidates
    whose confirm fetch proved them still live; ``unconfirmed`` counts candidates whose confirm
    fetch could not determine an answer (kept active); ``errored`` counts (bulk-relist-only) boards
    whose own list re-fetch could not be determined (no candidates are derived from an errored
    board -- mirrors ``sweep_boards``'s ``departed_ids(..., None) == set()`` guarantee).
    """
    target_boards = [(s, t) for s, t in boards if s in search_index_sources]
    if not target_boards:
        return {}

    sources_in_play = sorted({s for s, _ in target_boards})
    stored = _stored_active_rows_by_board(con, sources_in_play)
    counts = _new_search_index_counts(sources_in_play)

    write_lock = anyio.Lock()
    limiter = anyio.CapacityLimiter(max(1, concurrency))
    now_s = now()

    async def confirm_and_record(source: str, ref: DetailRef, job_id: str) -> None:
        async with limiter:
            verdict = await confirm_departed(ref, fetcher)
        async with write_lock:
            if verdict is None:
                counts[source]["unconfirmed"] += 1
            elif verdict is True:
                _expire_job_ids(con, [job_id], now_s)
                counts[source]["expired"] += 1
            else:
                counts[source]["confirmed_alive"] += 1

    async def process_bulk_relist(source: str, token: str) -> None:
        rows = stored.get((source, token), [])
        by_sid = {str(r["source_job_id"]): r for r in rows}
        async with limiter:
            live_ids = await board_live_ids(source, token, fetcher)
        async with write_lock:
            counts[source]["checked"] += 1
            if live_ids is None:
                counts[source]["errored"] += 1
                return
        candidates = departed_ids(set(by_sid.keys()), live_ids)
        if not candidates:
            return
        async with write_lock:
            counts[source]["candidates"] += len(candidates)
        async with anyio.create_task_group() as tg:
            for sid in candidates:
                row = by_sid[sid]
                tg.start_soon(confirm_and_record, source, DetailRef.from_row(row), str(row["id"]))

    async def process_per_posting(source: str, token: str) -> None:
        rows = stored.get((source, token), [])
        async with write_lock:
            counts[source]["checked"] += 1
        # Deterministic bound: a board with more stored active ids than the limit only gets its
        # first `board_active_id_limit` (by source_job_id, for a stable/testable slice) confirmed
        # this run -- the rest are picked up on a later run.
        bounded_rows = sorted(rows, key=lambda r: str(r["source_job_id"]))[:board_active_id_limit]
        if not bounded_rows:
            return
        async with write_lock:
            counts[source]["candidates"] += len(bounded_rows)
        async with anyio.create_task_group() as tg:
            for row in bounded_rows:
                tg.start_soon(confirm_and_record, source, DetailRef.from_row(row), str(row["id"]))

    async with anyio.create_task_group() as tg:
        for source, token in target_boards:
            if source in bulk_relist_confirm_sources:
                tg.start_soon(process_bulk_relist, source, token)
            elif source in per_posting_confirm_sources:
                tg.start_soon(process_per_posting, source, token)
            # else: a search_index_sources member routed to neither sub-set by a caller override
            # -- deliberately skipped rather than guessing a strategy for it.

    con.commit()
    return counts


async def sweep_all_boards(
    boards: Iterable[tuple[str, str]],
    con: sqlite3.Connection,
    fetcher: AsyncFetcher,
    *,
    deterministic_sources: frozenset[str] | set[str] = DETERMINISTIC_SOURCES,
    search_index_sources: frozenset[str] | set[str] = SEARCH_INDEX_SOURCES,
    concurrency: int = _FRESHNESS_CONCURRENCY,
    board_active_id_limit: int = _SEARCH_INDEX_BOARD_LIMIT,
    now: Callable[[], str],
) -> dict[str, dict[str, int]]:
    """Compose a full sweep run: Phase-0 ``sweep_boards`` (deterministic, single-miss expiry) AND
    Phase-1 ``sweep_search_index_boards`` (candidate + per-posting confirm) over the SAME board
    list. The two source sets are disjoint by construction (a source is either full-dump
    deterministic or reshuffling search-index, never both), so the two stats dicts never collide
    keys -- the merge is a plain union, not a per-key combine.

    Materializes ``boards`` once (an ``Iterable`` may be a one-shot generator) so both passes see
    the identical board list.
    """
    boards = list(boards)
    det_stats = await sweep_boards(
        boards,
        con,
        fetcher,
        deterministic_sources=deterministic_sources,
        concurrency=concurrency,
        now=now,
    )
    search_stats = await sweep_search_index_boards(
        boards,
        con,
        fetcher,
        search_index_sources=search_index_sources,
        concurrency=concurrency,
        board_active_id_limit=board_active_id_limit,
        now=now,
    )
    return {**det_stats, **search_stats}
