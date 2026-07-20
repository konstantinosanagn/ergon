"""Phase 3 of the daily freshness sweep: standalone CLI entry point.

Runs one host-shard's board-membership check against an ALREADY-BUILT index and writes the
confirmed-departed ids to a small sidecar sqlite db -- the PINNED contract
``ergon_tracker.index.build.apply_freshness_expiries`` (Phase 2) already knows how to carry
forward: ``expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)``.

Usage:
  python -m scripts.freshness_sweep --index dist/index.sqlite --out dist/index-freshness.sqlite
  # sharded (one shard of a 20-way host-sharded matrix, see freshness_shard.py):
  python -m scripts.freshness_sweep --index dist/index.sqlite --out dist/shard-03.sqlite \\
      --shard 3 --num-shards 20 --concurrency 32

THE INDEX PASSED VIA ``--index`` IS NEVER MUTATED: it is opened read-only for the whole run, only
to (1) enumerate this shard's active boards and (2) read the rows needed to re-verify them. The
real engine (``ergon_tracker.index.freshness.sweep_all_boards``) mutates status='active' rows to
status='expired' IN PLACE by design (see freshness.py's module docstring) -- so rather than fight
that contract, this CLI gives it an isolated, throwaway TEMP COPY (built via
``ergon_tracker.index.db.fresh_db`` + a row-for-row copy of just this shard's active jobs/
job_sources rows) to mutate, then reads back whichever rows the engine flipped to 'expired' as the
confirmed-departed set, and discards the temp copy. See ``_detect_departed`` below.

Each shard writes its OWN ``--out`` sidecar; merging shards into one published
``index-freshness.sqlite`` is a later phase (the workflow / merge step), not this CLI's job.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.index.db import connect, fresh_db  # noqa: E402
from ergon_tracker.index.freshness import (  # noqa: E402
    DETERMINISTIC_SOURCES,
    SEARCH_INDEX_SOURCES,
    BoardDelta,
    sweep_all_boards,
)
from ergon_tracker.index.freshness_shard import shard_boards  # noqa: E402
from ergon_tracker.providers.base import load_builtins  # noqa: E402

# The only sources the engine (`DETERMINISTIC_SOURCES | SEARCH_INDEX_SOURCES`) knows how to sweep
# -- boards on any other source are never selected, so a shard never wastes a slot on a board the
# engine would silently skip anyway.
_SWEPT_SOURCES: tuple[str, ...] = tuple(sorted(DETERMINISTIC_SOURCES | SEARCH_INDEX_SOURCES))

# Chunk size for parameterized `IN (...)` selects/copies -- mirrors freshness.py's `_UPDATE_CHUNK`,
# staying well under SQLite's bound-variable ceiling for a large shard.
_COPY_CHUNK = 500

_SIDECAR_SCHEMA = (
    "CREATE TABLE expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)"
)

# Phase 2 (delta-driven crawl) addition -- ALONGSIDE ``expired_ids`` (whose contract is unchanged).
# The per-board added-side change signal the daily build consumes to decide, cheaply, whether a
# board's membership moved since it was last crawled. One row per board that the sweep could
# DETERMINE a full, trustworthy live id-set for (deterministic sources only -- see
# ``ergon_tracker.index.freshness.sweep_all_boards``); a truncated/failed/undetermined fetch emits
# NO row for that board, never a partial fingerprint. ``added_ids`` is a JSON array of the raw
# ``source_job_id``s the board now lists that our index does not already hold active (sorted, so the
# serialization is stable/diffable); ``idset_hash`` is the stable SHA-1 fingerprint of the live
# id-SET (changes iff membership changes). PRIMARY KEY (source, board_token) so a re-run / merge is
# idempotent per board.
_BOARD_DELTAS_SCHEMA = (
    "CREATE TABLE board_deltas("
    "source TEXT NOT NULL, board_token TEXT NOT NULL, added_ids TEXT NOT NULL, "
    "idset_hash TEXT NOT NULL, computed_at TEXT NOT NULL, "
    "PRIMARY KEY (source, board_token))"
)

# Expiry-rate-monitor addition -- ALONGSIDE ``expired_ids``/``board_deltas`` (whose contracts are
# unchanged). This shard's OWN per-source counts (the dict ``sweep_all_boards`` returns), so the
# merge step can SUM them across every shard before evaluating the drift tripwire
# (``ergon_tracker.index.freshness.check_expiry_alarms``) on the true, cross-shard totals -- a
# single shard only ever sees a slice of a source's boards, so its own rate is not a trustworthy
# per-source signal in isolation (see that function's docstring). Columns cover the UNION of both
# counts shapes this module produces (deterministic: checked/departed/expired/errored;
# search-index: checked/candidates/expired/confirmed_alive/unconfirmed/errored) -- a shape missing
# a given key contributes 0 for it (see ``_stats_rows``). One row per source PER SHARD (not a
# cross-source PRIMARY KEY): a shard's own board partition means a given source's counts here are
# already that shard's total for it, but the merge still needs to SUM across shards, not dedup.
_SOURCE_STATS_SCHEMA = (
    "CREATE TABLE source_stats("
    "source TEXT NOT NULL, checked INTEGER NOT NULL, candidates INTEGER NOT NULL, "
    "departed INTEGER NOT NULL, expired INTEGER NOT NULL, confirmed_alive INTEGER NOT NULL, "
    "unconfirmed INTEGER NOT NULL, errored INTEGER NOT NULL)"
)


def _active_boards(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every distinct ``(source, board_token)`` with at least one ``status='active'`` row, read
    from the index ``con`` opened READ-ONLY by the caller -- restricted to `_SWEPT_SOURCES` so an
    unrecognized source's boards never enter the shard partition at all."""
    placeholders = ",".join("?" for _ in _SWEPT_SOURCES)
    rows = con.execute(
        "SELECT DISTINCT source, board_token FROM jobs "
        f"WHERE status='active' AND board_token IS NOT NULL AND source IN ({placeholders})",  # noqa: S608
        _SWEPT_SOURCES,
    ).fetchall()
    return [(str(r["source"]), str(r["board_token"])) for r in rows]


def _copy_boards_into(
    dst: sqlite3.Connection, src: sqlite3.Connection, boards: list[tuple[str, str]]
) -> None:
    """Copy every ``status='active'`` ``jobs`` row (+ its ``job_sources`` rows) for ``boards`` from
    the read-only ``src`` index into the writable, freshly-created (empty) ``dst`` temp copy, so
    ``sweep_all_boards`` can mutate ``dst`` in place without ``src`` ever being touched.

    Column lists are read off each SELECT's own ``cursor.description`` (not hardcoded) so the copy
    stays correct across any schema drift between this CLI and the index's actual columns --
    ``dst`` was built by ``fresh_db`` from the SAME package's ``schema.sql``, so the column sets
    always match.
    """
    dst.execute("PRAGMA foreign_keys = OFF")  # dst.companies is intentionally never populated

    by_source: dict[str, list[str]] = {}
    for source, token in boards:
        by_source.setdefault(source, []).append(token)

    job_ids: list[str] = []
    for source, tokens in by_source.items():
        for start in range(0, len(tokens), _COPY_CHUNK):
            chunk = tokens[start : start + _COPY_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            cur = src.execute(
                "SELECT * FROM jobs WHERE status='active' AND source=? "
                f"AND board_token IN ({placeholders})",  # noqa: S608
                [source, *chunk],
            )
            cols = [d[0] for d in cur.description]
            col_list = ",".join(cols)
            ph = ",".join("?" for _ in cols)
            for row in cur.fetchall():
                dst.execute(f"INSERT INTO jobs ({col_list}) VALUES ({ph})", tuple(row))  # noqa: S608
                job_ids.append(str(row["id"]))

    for start in range(0, len(job_ids), _COPY_CHUNK):
        chunk = job_ids[start : start + _COPY_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        cur = src.execute(
            f"SELECT * FROM job_sources WHERE job_id IN ({placeholders})",
            chunk,  # noqa: S608
        )
        cols = [d[0] for d in cur.description]
        col_list = ",".join(cols)
        ph = ",".join("?" for _ in cols)
        for row in cur.fetchall():
            dst.execute(f"INSERT INTO job_sources ({col_list}) VALUES ({ph})", tuple(row))  # noqa: S608
    dst.commit()


def _delta_rows(deltas: dict[tuple[str, str], BoardDelta]) -> list[tuple[str, str, str, str, str]]:
    """Serialize the engine's per-board ``BoardDelta`` map into sidecar rows: ``added_ids`` becomes
    a JSON array of the raw ``source_job_id``s, SORTED so the serialization is stable and diffable
    (mirrors ``idset_hash``'s own sort-then-hash) and two runs with the same membership produce a
    byte-identical row."""
    return [
        (
            d.source,
            d.board_token,
            json.dumps(sorted(d.added_ids)),
            d.idset_hash,
            d.computed_at,
        )
        for d in deltas.values()
    ]


_STATS_KEYS: tuple[str, ...] = (
    "checked",
    "candidates",
    "departed",
    "expired",
    "confirmed_alive",
    "unconfirmed",
    "errored",
)


_StatsRow = tuple[str, int, int, int, int, int, int, int]


def _stats_rows(stats: dict[str, dict[str, int]]) -> list[_StatsRow]:
    """Serialize ``sweep_all_boards``'s per-source counts into ``source_stats`` rows, in
    ``_STATS_KEYS`` column order -- a key absent from a given source's counts shape (deterministic
    lacks ``candidates``/``confirmed_alive``/``unconfirmed``; search-index lacks ``departed``)
    contributes 0 rather than erroring, per :func:`ergon_tracker.index.freshness.source_expiry_rate`'s
    own ``.get(..., 0)`` convention."""
    rows: list[_StatsRow] = []
    for source, counts in sorted(stats.items()):
        checked, candidates, departed, expired, confirmed_alive, unconfirmed, errored = (
            counts.get(k, 0) for k in _STATS_KEYS
        )
        rows.append(
            (source, checked, candidates, departed, expired, confirmed_alive, unconfirmed, errored)
        )
    return rows


def _write_sidecar(
    out_path: Path,
    rows: list[tuple[str, str, str | None]],
    delta_rows: list[tuple[str, str, str, str, str]] | None = None,
    stats_rows: list[tuple[str, int, int, int, int, int, int, int]] | None = None,
) -> None:
    """Write the PINNED freshness sidecar contract exactly (see
    ``ergon_tracker.index.build.apply_freshness_expiries``'s docstring, which the daily build's
    carry-forward reads against byte-for-byte): one table ``expired_ids(id TEXT PRIMARY KEY,
    expired_at TEXT NOT NULL, reason TEXT)`` -- UNCHANGED -- PLUS the Phase-2 ``board_deltas`` table
    (see ``_BOARD_DELTAS_SCHEMA``) carrying the per-board added-side change signal, PLUS the
    expiry-rate-monitor's ``source_stats`` table (see ``_SOURCE_STATS_SCHEMA``) carrying this
    shard's own per-source counts for the merge step to sum. Always creates all three tables even
    with zero rows, so a downstream merge/build step always sees a well-formed sidecar rather than
    special-casing an empty shard."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    con = sqlite3.connect(str(out_path))
    try:
        con.execute(_SIDECAR_SCHEMA)
        con.execute(_BOARD_DELTAS_SCHEMA)
        con.execute(_SOURCE_STATS_SCHEMA)
        con.executemany("INSERT INTO expired_ids(id, expired_at, reason) VALUES (?,?,?)", rows)
        con.executemany(
            "INSERT INTO board_deltas(source, board_token, added_ids, idset_hash, computed_at) "
            "VALUES (?,?,?,?,?)",
            delta_rows or [],
        )
        con.executemany(
            "INSERT INTO source_stats(source, checked, candidates, departed, expired, "
            "confirmed_alive, unconfirmed, errored) VALUES (?,?,?,?,?,?,?,?)",
            stats_rows or [],
        )
        con.commit()
    finally:
        con.close()


async def _detect_departed(
    index_path: Path, boards: list[tuple[str, str]], *, concurrency: int
) -> tuple[
    dict[str, dict[str, int]],
    list[tuple[str, str, str | None]],
    dict[tuple[str, str], BoardDelta],
]:
    """DETECT confirmed-departed ids AND the per-board added-side change signal WITHOUT ever
    mutating the real index at ``index_path``.

    Opens ``index_path`` read-only, copies ``boards``' active rows into an isolated, throwaway
    temp copy of the index schema (``fresh_db``), runs the real engine (``sweep_all_boards``)
    against THAT copy (its ``status='expired'`` UPDATEs land only in the temp copy), then reads
    back whichever rows it flipped -- the confirmed-departed set -- as ``(id, expired_at, reason)``
    rows ready for the sidecar. The temp copy is always discarded, regardless of outcome.

    The engine ALSO fills the ``board_deltas`` collector we hand it (only for boards it could
    determine a full, trustworthy live id-set for -- deterministic sources; a truncated/failed
    fetch is simply absent), returned as-is for the sidecar's ``board_deltas`` table.

    Per-board failures are already non-fatal inside the engine itself (``board_live_ids``/
    ``confirm_departed`` never raise -- an errored board just contributes 0 departures and an
    ``errored`` count); nothing extra is needed here for that guarantee.
    """
    load_builtins()
    src = connect(index_path, read_only=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="freshness-shard-"))
    try:
        tmp_path = tmp_dir / "shard-copy.sqlite"
        fresh_db(tmp_path)
        dst = connect(tmp_path)
        try:
            _copy_boards_into(dst, src, boards)
            deltas: dict[tuple[str, str], BoardDelta] = {}
            async with AsyncFetcher(concurrency=concurrency) as fetcher:
                stats = await sweep_all_boards(
                    boards,
                    dst,
                    fetcher,
                    concurrency=concurrency,
                    board_deltas=deltas,
                    now=lambda: datetime.now(timezone.utc).isoformat(),
                )
            expired_rows = dst.execute(
                "SELECT id, expired_at, expiry_reason FROM jobs WHERE status='expired'"
            ).fetchall()
            departed = [
                (str(r["id"]), str(r["expired_at"]), r["expiry_reason"]) for r in expired_rows
            ]
            return stats, departed, deltas
        finally:
            dst.close()
    finally:
        src.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _log_stats(stats: dict[str, dict[str, int]]) -> None:
    for source in sorted(stats):
        parts = " ".join(f"{k}={v}" for k, v in stats[source].items())
        print(f"[freshness] {source}: {parts}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Daily freshness sweep: detect confirmed-departed board-membership ids for one "
            "host-shard and write them to a sidecar sqlite db. Never mutates --index."
        )
    )
    parser.add_argument(
        "--index", required=True, type=Path, help="Path to the built index.sqlite (read-only)."
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="Path to write this shard's expired_ids sidecar."
    )
    parser.add_argument(
        "--shard", type=int, default=0, help="This shard's 0-based index (default 0)."
    )
    parser.add_argument(
        "--num-shards", type=int, default=1, help="Total number of shards (default 1)."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=32,
        help="In-flight board-fetch concurrency cap, also AsyncFetcher's global cap (default 32).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.index.exists():
        print(f"[freshness] index not found: {args.index}", file=sys.stderr)
        return 1

    con = connect(args.index, read_only=True)
    try:
        all_boards = _active_boards(con)
    finally:
        con.close()

    try:
        this_shard = shard_boards(all_boards, args.shard, args.num_shards)
    except ValueError as exc:
        print(f"[freshness] invalid shard arguments: {exc}", file=sys.stderr)
        return 2

    print(
        f"[freshness] shard {args.shard}/{args.num_shards}: {len(this_shard)}/{len(all_boards)} boards"
    )

    if not this_shard:
        _write_sidecar(args.out, [])
        print(f"[freshness] no boards on this shard; wrote empty sidecar -> {args.out}")
        return 0

    import anyio

    stats, departed, deltas = anyio.run(
        lambda: _detect_departed(args.index, this_shard, concurrency=args.concurrency)
    )
    _log_stats(stats)
    delta_rows = _delta_rows(deltas)
    stats_rows = _stats_rows(stats)
    _write_sidecar(args.out, departed, delta_rows, stats_rows)
    print(
        f"[freshness] wrote {len(departed)} departed ids, {len(delta_rows)} board deltas, "
        f"{len(stats_rows)} source-stats rows -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
