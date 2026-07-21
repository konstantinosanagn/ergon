"""Build-time job-posting LIVENESS pass: detect apply-URL dead links and flip them to
``status='expired'`` -- which every query path already filters (``index/query.py``'s
``j.status = 'active'`` gate), so a flip makes the row invisible with zero query-layer changes.

WHY: research measured dejobs apply-links are 52.7% dead (an order of magnitude worse than any
other source), and nothing in the pipeline ever re-checks a listed posting's liveness -- a source
that still *lists* a job is served ``status='active'`` forever, even after the posting is gone.

Mirrors ``index/detail.py``'s Tier-3 sidecar architecture: a small sqlite sidecar
(``job_liveness``) keyed by posting id, an injected async fetch callable so the reconcile is
network-agnostic (and therefore fully offline-testable), and a bounded/windowed pass so a single
CI run never has to re-check the whole index. The dispatch glue (``get_provider`` + a real
``AsyncFetcher``) lives in ``scripts/build_index.py`` (``_reconcile_liveness`` /
``build_and_publish_liveness``), exactly where ``detail.py``'s analogous glue
(``_reconcile_detail`` / ``build_and_publish_detail``) lives -- this module never imports the
network stack.

TWO-STAGE VERDICT (driven by measured false-positive data -- list-membership alone is unreliable
for search-index-style boards):

  Stage 1 (candidate): rows are grouped by (source, board token) and each board is re-fetched
  ONCE (``fetch_board``) -- a row is a *candidate* when its computed job id is absent from the
  fresh board's id set.

  Stage 2 (confirm): a candidate on a source whose provider implements a real ``fetch_detail``
  (``CONFIRM_VIA_DETAIL_SOURCES`` -- their list APIs reshuffle/paginate non-deterministically,
  measured 50-100% list-miss false-positive rates) is confirmed via a per-posting detail fetch
  instead of trusting the list miss: an explicit failure (``fetch_detail``'s contract is
  non-raising -- ``None``/empty means dead) flips it immediately; a successful detail fetch means
  the list miss was a false positive, and the row is left alive. Sources WITHOUT a ``fetch_detail``
  (proven 0-5% list false-positive rate) instead require ``dead_streak >= 2`` -- two consecutive
  WEEKLY misses -- before flipping, as transient-blip insurance (a board glitch on one crawl must
  not delist a live posting).

Eligibility to (re-)check a row is purely TIME-based (``checked_at`` older than ``recheck_days``):
a dead link produces no content change, so gating on a content sig (like the detail sidecar does)
would never re-select an already-checked-and-still-listed dead row.

ACTION: a confirmed-dead row is UPDATEd in place (``status='expired', expired_at=...,
expiry_reason='dead_link'``) -- never hard-deleted, so ``COUNT(*) FROM jobs`` is unchanged by this
pass and the row_floor publish gate (``gates.py``) can never trip on it.
"""

from __future__ import annotations

import os
import sqlite3
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

import anyio

from ..models import DetailFetch
from ..registry.store import SeedRegistry
from .detail import DetailRef

__all__ = [
    "LIVENESS_SCHEMA_VERSION",
    "RECHECK_DAYS",
    "CONFIRM_VIA_DETAIL_SOURCES",
    "ensure_liveness_schema",
    "open_liveness",
    "reconcile_liveness_tier",
]

LIVENESS_SCHEMA_VERSION = 1
LIVENESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_liveness (
  id TEXT PRIMARY KEY,
  checked_at TEXT,
  dead_streak INTEGER NOT NULL DEFAULT 0,
  verdict TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ensure_liveness_schema(con: sqlite3.Connection) -> None:
    con.executescript(LIVENESS_SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(LIVENESS_SCHEMA_VERSION),),
    )
    con.commit()


def open_liveness(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    ensure_liveness_schema(con)
    return con


# Recheck cadence: a row isn't re-selected until its last check is at least this many days old.
# Dead links produce NO content change, so (unlike the detail sidecar) eligibility here is purely
# time-based, never sig-gated.
RECHECK_DAYS = int(os.environ.get("ERGON_LIVENESS_RECHECK_DAYS", "7"))

# In-flight board fetches (env-tunable, mirrors detail.py's ERGON_DETAIL_CONCURRENCY). Politeness
# is enforced BELOW this by the injected AsyncFetcher's own per-host token-bucket -- this only
# bounds how many boards' worth of (board-fetch + any stage-2 confirm fetches) run concurrently.
_LIVENESS_CONCURRENCY = int(os.environ.get("ERGON_LIVENESS_CONCURRENCY", "24"))

# Sources whose provider implements a real fetch_detail -- their LIST apis reshuffle/paginate
# non-deterministically (measured 50-100% list-miss false-positive rates), so a list-miss on one
# of these is only a *candidate*: it must be confirmed via a per-posting detail fetch before being
# flipped dead. Mirrors scripts/build_index.py's _TIER3_DETAIL_SOURCES (kept in sync manually --
# both enumerate "sources with a working fetch_detail", the same underlying fact from two call
# sites: the Tier-3 JD-recovery pass and this liveness pass).
CONFIRM_VIA_DETAIL_SOURCES: tuple[str, ...] = (
    "smartrecruiters",
    "workday",
    "oracle",
    "icims",
    "successfactors",
    "eightfold",
    "rippling",
    "radancy",
    "workable",
    "join",
    "phenom",
    "bamboohr",
    "ukg",
    "jobvite",
    "themuse",
    "adp",
    "avature",
    "taleobe",
    # Confirm sources with a real fetch_detail + a clean gone-signal (2026-07 live recon):
    "brassring",  # JobDetails AJAX record; HTTP 404 (or null Jobdetails) for a removed jobid.
    "peopleadmin",  # /postings/{id}; 302 -> /postings search root, or 404, for a removed posting.
    "lever",  # per-posting API; textbook HTTP 404 {"ok":false,"error":"Document not found"}.
)

# A source WITHOUT fetch_detail needs this many consecutive weekly misses before a candidate
# flips dead (transient-blip insurance). A CONFIRM_VIA_DETAIL_SOURCES candidate instead needs only
# ONE confirmed-dead detail fetch (the detail fetch itself IS the confirmation).
_STREAK_THRESHOLD_UNCONFIRMED = 2
_STREAK_THRESHOLD_CONFIRMED = 1

_JOBS_COLUMNS = (
    "id, source, board_token, company_key, apply_url, listing_url, content_hash, title, level"
)


def _load_existing(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = con.execute("SELECT id, checked_at, dead_streak, verdict FROM job_liveness").fetchall()
    return {r[0]: {"checked_at": r[1], "dead_streak": r[2], "verdict": r[3]} for r in rows}


def _eligible(
    job_id: str, now_s: str, recheck_days: int, existing: dict[str, dict[str, Any]]
) -> bool:
    """A row needs (re-)checking when it's never been checked, or its last check is older than
    ``recheck_days`` -- purely time-based (see module docstring: a dead link has no content-sig
    signal to gate on)."""
    prev = existing.get(job_id)
    if prev is None or not prev.get("checked_at"):
        return True
    try:
        last = datetime.fromisoformat(prev["checked_at"])
        now_dt = datetime.fromisoformat(now_s)
    except (TypeError, ValueError):
        return True  # unparseable stored timestamp -- treat as never-checked, fail open to re-check
    return (now_dt - last) >= timedelta(days=recheck_days)


def _resolve_token(row: dict[str, Any], registry: SeedRegistry) -> str | None:
    """The board token to re-fetch this row's board with: the row's own ``board_token`` (see
    ``mapping.to_row`` -- populated for every row crawled after the board_token prereq fix)
    when present, else a fallback lookup via ``SeedRegistry.get(company_key)['token']`` for
    legacy/carried-forward rows that predate that fix.

    The fallback only trusts the registry entry when its ``ats`` still matches this row's
    ``source`` -- a company that has since moved ATS would otherwise resolve to the WRONG board.
    """
    token = row.get("board_token")
    if token:
        return str(token)
    key = row.get("company_key")
    if not key:
        return None
    entry = registry.get(key)
    if not entry:
        return None
    if entry.get("ats") != row.get("source"):
        return None
    resolved = entry.get("token")
    return str(resolved) if resolved else None


def _detail_confirms_alive(result: str | DetailFetch | None) -> bool:
    """Stage-2 confirm verdict from a ``fetch_detail`` result: alive iff it carries non-empty
    text. Under ``fetch_detail``'s return/raise contract (``providers/base.py``) a returned
    ``None``/empty is a DEFINITIVE not-found (404/gone); a raised exception is indeterminate and is
    handled by the caller (KEEP the row), never passed here as ``None``."""
    if isinstance(result, DetailFetch):
        return bool(result.text)
    return bool(result)


def _record_liveness(
    con: sqlite3.Connection, job_id: str, checked_at: str, *, dead_streak: int, verdict: str
) -> None:
    con.execute(
        "INSERT INTO job_liveness(id, checked_at, dead_streak, verdict) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET checked_at = excluded.checked_at, "
        "dead_streak = excluded.dead_streak, verdict = excluded.verdict",
        (job_id, checked_at, dead_streak, verdict),
    )


def _expire_row(con: sqlite3.Connection, job_id: str, expired_at: str) -> None:
    """Flip a confirmed-dead row to expired IN PLACE -- never hard-deleted (unlike
    ``build._purge_ancient``'s unambiguous-dead-tail purge), so ``COUNT(*) FROM jobs`` is
    unaffected and the row_floor publish gate can never trip on this pass."""
    con.execute(
        "UPDATE jobs SET status = 'expired', expired_at = ?, expiry_reason = 'dead_link' "
        "WHERE id = ?",
        (expired_at, job_id),
    )


def _load_cursor(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT value FROM meta WHERE key = 'liveness_cursor'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _save_cursor(con: sqlite3.Connection, cursor: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('liveness_cursor', ?)", (str(cursor),)
    )


def _select_board_window(
    con: sqlite3.Connection, board_keys: list[tuple[str, str]], max_boards: int | None
) -> tuple[list[tuple[str, str]], int]:
    """Rotating-cursor slice of ``board_keys`` (wrapping), so repeated bounded runs eventually
    reach every due board rather than always the same head of the list -- mirrors
    ``detail.py::_select_window``, at board granularity instead of per-ref."""
    total = len(board_keys)
    if total == 0:
        return [], 0
    if max_boards is None or max_boards >= total:
        return board_keys, 0
    cursor = _load_cursor(con) % total
    window = [board_keys[(cursor + i) % total] for i in range(max_boards)]
    return window, (cursor + max_boards) % total


async def reconcile_liveness_tier(
    liveness_path: str,
    index_path: str,
    *,
    fetch_board: Callable[[str, str], Awaitable[set[str] | None]],
    fetch_detail: Callable[[DetailRef], Awaitable[str | DetailFetch | None]],
    now: Callable[[], str],
    recheck_days: int = RECHECK_DAYS,
    max_boards: int | None = None,
    concurrency: int = _LIVENESS_CONCURRENCY,
    confirm_sources: Sequence[str] = CONFIRM_VIA_DETAIL_SOURCES,
) -> dict[str, int]:
    """The liveness reconcile pass: select due ``active`` rows, group by resolved board, re-fetch
    each due board ONCE, classify every row on it (two-stage verdict, see module docstring), and
    flip confirmed-dead rows to ``status='expired'`` directly on the ``jobs`` table -- there is no
    separate merge step (unlike the detail tier): the sidecar here only tracks recheck cadence
    (``checked_at``/``dead_streak``/``verdict``), it never carries a value that needs merging.

    ``fetch_board(source, token)`` must return the set of computed job ids currently on that
    board, or ``None`` on a failed/errored fetch -- a ``None`` board is SKIPPED entirely this run
    (every row on it stays untouched, still eligible next run) so a transient board-fetch failure
    can never be mistaken for "every posting on this board disappeared".

    ``fetch_detail`` is the Stage-2 confirm dispatcher (same shape as ``detail.py``'s
    ``reconcile_detail_tier`` parameter of the same name); both are injected so this whole pass is
    network-agnostic and fully offline-testable. ``now`` is likewise injected (no wall-clock reads
    here).

    ``max_boards`` bounds how many DUE boards this run processes (rotating cursor, like
    ``detail.py``'s windowed drain) -- ``None`` processes every due board in one pass.
    """
    liv_con = open_liveness(liveness_path)
    try:
        idx_con = sqlite3.connect(index_path)
        idx_con.row_factory = sqlite3.Row
        try:
            rows = [
                dict(r)
                for r in idx_con.execute(
                    f"SELECT {_JOBS_COLUMNS} FROM jobs WHERE status = 'active'"  # noqa: S608
                ).fetchall()
            ]

            existing = _load_existing(liv_con)
            now_s = now()
            registry = SeedRegistry()

            due_rows = [r for r in rows if _eligible(r["id"], now_s, recheck_days, existing)]

            groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
            unresolved = 0
            for r in due_rows:
                token = _resolve_token(r, registry)
                if not token:
                    unresolved += 1
                    continue
                groups.setdefault((str(r["source"]), token), []).append(r)

            board_keys = sorted(groups.keys())  # deterministic order for a meaningful rotation
            window_keys, next_cursor = _select_board_window(liv_con, board_keys, max_boards)
            _save_cursor(liv_con, next_cursor)
            liv_con.commit()

            counts = {
                "checked": 0,
                "flipped_dead": 0,
                "confirmed_alive": 0,
                "confirm_errored": 0,
                "boards_fetched": 0,
                "boards_failed": 0,
                "unresolved": unresolved,
            }
            # Single writer lock: board-fetch and stage-2 confirm-fetch network calls run
            # concurrently (bounded by `concurrency`), but every sqlite write (sidecar record +
            # jobs.status flip) is serialized through this lock -- mirrors detail.py's
            # single-consumer-writes-serially guarantee without needing its stream/consumer
            # plumbing, since here (unlike detail's per-ref pipeline) writes are cheap and
            # scattered across many small per-board batches rather than one hot consumer loop.
            write_lock = anyio.Lock()
            limiter = anyio.CapacityLimiter(concurrency)

            async def classify_row(r: dict[str, Any], fresh_ids: set[str], tier3: bool) -> None:
                rid = str(r["id"])
                if rid in fresh_ids:
                    async with write_lock:
                        _record_liveness(liv_con, rid, now_s, dead_streak=0, verdict="live")
                        counts["checked"] += 1
                    return
                prev_streak = int(existing.get(rid, {}).get("dead_streak") or 0)
                if tier3:
                    ref = DetailRef.from_row(r)
                    # A RAISED fetch_detail is INDETERMINATE (transient/unbuildable), never
                    # evidence of death -- under the hardened contract (providers/base.py) only a
                    # returned None means a definitive 404/gone. Distinguish the two: on a raise,
                    # KEEP the row and leave its streak untouched (retry next run), exactly like
                    # freshness.py's confirm_departed. Collapsing a raise to None here (the old
                    # behavior) would expire a still-live posting on a single transient blip, since
                    # _STREAK_THRESHOLD_CONFIRMED == 1.
                    confirm_errored = False
                    try:
                        result = await fetch_detail(ref)
                    except Exception:  # noqa: BLE001 - indeterminate confirm, never a death signal
                        confirm_errored = True
                        result = None
                    if confirm_errored:
                        async with write_lock:
                            counts["confirm_errored"] += 1
                            counts["checked"] += 1
                        return
                    confirmed_alive = _detail_confirms_alive(result)
                    async with write_lock:
                        if confirmed_alive:
                            # List-miss was a false positive (reshuffled/paginated list) -- the
                            # detail fetch proves the posting is still live. Reset the streak.
                            _record_liveness(liv_con, rid, now_s, dead_streak=0, verdict="live")
                            counts["confirmed_alive"] += 1
                        else:
                            # The detail fetch itself IS the confirmation, so a single miss
                            # suffices here (unlike the unconfirmed streak-gated path below) --
                            # _STREAK_THRESHOLD_CONFIRMED == 1 makes that explicit rather than
                            # flipping unconditionally.
                            streak = prev_streak + 1
                            verdict = (
                                "dead" if streak >= _STREAK_THRESHOLD_CONFIRMED else "candidate"
                            )
                            _record_liveness(
                                liv_con, rid, now_s, dead_streak=streak, verdict=verdict
                            )
                            if verdict == "dead":
                                _expire_row(idx_con, rid, now_s)
                                counts["flipped_dead"] += 1
                        counts["checked"] += 1
                    return
                streak = prev_streak + 1
                async with write_lock:
                    if streak >= _STREAK_THRESHOLD_UNCONFIRMED:
                        _record_liveness(liv_con, rid, now_s, dead_streak=streak, verdict="dead")
                        _expire_row(idx_con, rid, now_s)
                        counts["flipped_dead"] += 1
                    else:
                        _record_liveness(
                            liv_con, rid, now_s, dead_streak=streak, verdict="candidate"
                        )
                    counts["checked"] += 1

            async def process_board(key: tuple[str, str]) -> None:
                source, token = key
                board_rows = groups[key]
                async with limiter:
                    fresh_ids = await fetch_board(source, token)
                if fresh_ids is None:
                    async with write_lock:
                        counts["boards_failed"] += 1
                    return  # transient board failure -- leave every row on it untouched this run
                async with write_lock:
                    counts["boards_fetched"] += 1
                tier3 = source in confirm_sources
                for r in board_rows:
                    await classify_row(r, fresh_ids, tier3)

            async with anyio.create_task_group() as tg:
                for key in window_keys:
                    tg.start_soon(process_board, key)

            idx_con.commit()
            liv_con.commit()

            existing_after = _load_existing(liv_con)
            remaining_eligible = sum(
                1 for r in rows if _eligible(r["id"], now_s, recheck_days, existing_after)
            )
            counts["remaining_eligible"] = remaining_eligible
            return counts
        finally:
            idx_con.close()
    finally:
        liv_con.close()
