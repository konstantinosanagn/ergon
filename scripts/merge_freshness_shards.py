#!/usr/bin/env python3
"""Combine per-shard freshness-sweep sidecars into one ``index-freshness.sqlite``.

The daily host-sharded freshness matrix (``.github/workflows/freshness-sweep.yml``) runs 20
parallel ``python -m scripts.freshness_sweep`` jobs, each producing its own
``index-freshness-shard-N.sqlite`` covering a DISJOINT slice of boards (every board's politeness
bucket hashes to exactly ONE shard -- see ``ergon_tracker.index.freshness_shard.shard_boards``).
The workflow's separate ``merge`` job runs this script to union all 20 shard sidecars back into a
single combined ``index-freshness.sqlite``, which is then gzipped and published to the
``index-latest`` release. The next daily ``build-index.yml`` run downloads that combined sidecar
and carries its expiries forward via ``ergon_tracker.index.build.apply_freshness_expiries`` --
this script never touches the core index itself.

Usage:
  uv run python scripts/merge_freshness_shards.py --shards-dir dist/shards \\
      --out dist/index-freshness.sqlite

Each shard sidecar is the PINNED contract ``scripts/freshness_sweep.py`` writes (and
``ergon_tracker.index.build.apply_freshness_expiries`` reads): one table
``expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)``. That DDL is
duplicated here as a literal (rather than imported from ``scripts.freshness_sweep``) because this
script is invoked as a direct file path (``python scripts/merge_freshness_shards.py``, mirroring
``merge_detail_shards.py``/``merge_vectors_shards.py``) -- under that invocation ``sys.path[0]`` is
the ``scripts/`` directory itself, not the repo root, so ``import scripts.freshness_sweep`` would
not resolve. Otherwise stdlib only (sqlite3, argparse, glob via ``Path.glob``) -- no dependency on
``ergon_tracker`` at all, unlike its detail/vectors siblings.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_SHARD_GLOB = "index-freshness-shard-*.sqlite"

# Pinned contract -- MUST stay byte-for-byte identical (modulo IF NOT EXISTS, needed here since
# the output db may already carry earlier shards' rows across merge calls) to
# `scripts/freshness_sweep.py`'s `_SIDECAR_SCHEMA` and the table shape
# `ergon_tracker.index.build.apply_freshness_expiries` ATTACHes and reads. If that contract ever
# changes, update all three together.
_SIDECAR_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS expired_ids"
    "(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)"
)

# Phase 2 (delta-driven crawl) addition -- MUST stay byte-for-byte identical (modulo IF NOT EXISTS)
# to `scripts/freshness_sweep.py`'s `_BOARD_DELTAS_SCHEMA`. The per-board added-side change signal
# is unioned across shards exactly like `expired_ids`: shards partition boards disjointly (every
# board hashes to one shard), so a given (source, board_token) appears in at most one shard; the
# PRIMARY KEY (source, board_token) makes a re-run / coincidental overlap idempotent under
# INSERT OR IGNORE. Merged alongside `expired_ids`, in the SAME per-shard transaction.
_BOARD_DELTAS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS board_deltas"
    "(source TEXT NOT NULL, board_token TEXT NOT NULL, added_ids TEXT NOT NULL, "
    "idset_hash TEXT NOT NULL, computed_at TEXT NOT NULL, "
    "PRIMARY KEY (source, board_token))"
)


def find_shard_dbs(shards_dir: Path) -> list[Path]:
    """Shard files in ``shards_dir``, sorted for a deterministic, reproducible merge order."""
    return sorted(shards_dir.glob(_SHARD_GLOB))


def _union_table(con: sqlite3.Connection, insert_sql: str, shard_name: str) -> int:
    """Run one ``INSERT OR IGNORE ... SELECT ... FROM shard.<table>`` in its OWN transaction (commit
    on success) and return rows added. Committing per-union is what keeps the two tables INDEPENDENT:
    a shard MISSING one table (an older shard predating it, or a truncated artifact) fails only that
    union -- rolled back and skipped with a warning, 0 rows -- while the sibling table it DID carry
    was already committed and is never undone."""
    before = con.total_changes
    try:
        con.execute(insert_sql)
        con.commit()
    except sqlite3.Error as exc:  # missing/truncated table inside the shard -> 0 rows, don't abort
        print(f"  WARNING: could not read a table in {shard_name}: {exc}", file=sys.stderr)
        con.rollback()
        return 0
    return con.total_changes - before


def merge_shards(shard_paths: list[Path], out_path: Path) -> dict[str, int]:
    """Union every shard's ``expired_ids`` AND ``board_deltas`` rows into ``out_path`` (both schemas
    ensured via the pinned ``_SIDECAR_SCHEMA`` / ``_BOARD_DELTAS_SCHEMA`` DDL). Returns
    ``{shard_filename: expired_rows_merged, ..., "_total": total_expired,
    "_total_deltas": total_delta_rows}`` -- per-shard values report the ``expired_ids`` count (the
    long-standing contract); the delta total is reported separately so the ``expired_ids`` numbers
    callers already parse are unchanged.

    Row union: shards partition boards disjointly by host/rate-bucket (see
    ``freshness_shard.shard_boards``), so two DIFFERENT shards should never confirm-depart the
    SAME posting id NOR emit a delta for the SAME (source, board_token) -- but even if they did,
    any duplicate row is semantically IDENTICAL. So both writes are ``INSERT OR IGNORE``: the first
    shard (in sorted path order) to claim a key wins, later duplicates are silently dropped rather
    than erroring -- order-INSENSITIVE for correctness, and safe to re-run the merge (e.g. after a
    retry) without erroring on a re-processed shard.

    Rows are streamed via ATTACH + ``INSERT ... SELECT`` (never ``fetchall``'d into Python), so
    peak memory is O(1) per shard regardless of how many ids/deltas a shard produced.

    Resilience: a shard whose file can't be ATTACHed (a truncated or empty artifact download) is
    skipped with a warning rather than aborting the whole merge; a shard missing just ONE of the
    two tables still contributes the table it does have (each table's union is guarded
    independently, see ``_union_table``). One bad shard never loses the other 19 (mirrors
    ``merge_vectors_shards.py``'s per-shard resilience).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out_path))
    stats: dict[str, int] = {}
    total = 0
    total_deltas = 0
    try:
        con.execute(_SIDECAR_SCHEMA)
        con.execute(_BOARD_DELTAS_SCHEMA)
        for shard_path in shard_paths:
            try:
                con.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
            except sqlite3.Error as exc:  # unreadable / not-a-db artifact -> skip, don't abort
                print(f"  WARNING: could not attach {shard_path.name}: {exc}", file=sys.stderr)
                continue
            try:
                # Disjoint by construction (see docstring), so OR IGNORE never silently drops a
                # real cross-shard disagreement -- it only makes a re-run / coincidental overlap
                # idempotent instead of raising a PRIMARY KEY conflict. Each table's union is
                # independently guarded so a shard carrying only one of the two still contributes it.
                n = _union_table(
                    con,
                    "INSERT OR IGNORE INTO main.expired_ids (id, expired_at, reason) "
                    "SELECT id, expired_at, reason FROM shard.expired_ids",
                    shard_path.name,
                )
                nd = _union_table(
                    con,
                    "INSERT OR IGNORE INTO main.board_deltas "
                    "(source, board_token, added_ids, idset_hash, computed_at) "
                    "SELECT source, board_token, added_ids, idset_hash, computed_at "
                    "FROM shard.board_deltas",
                    shard_path.name,
                )
                stats[shard_path.name] = n
                total += n
                total_deltas += nd
            finally:
                # Each _union_table already committed (or rolled back) its own transaction, so
                # there is no open transaction here -- DETACH is safe.
                con.execute("DETACH DATABASE shard")
        con.commit()
    finally:
        con.close()
    stats["_total"] = total
    stats["_total_deltas"] = total_deltas
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--shards-dir",
        type=Path,
        required=True,
        help=f"Directory containing downloaded {_SHARD_GLOB} artifacts",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Combined output sidecar path (e.g. dist/index-freshness.sqlite)",
    )
    args = parser.parse_args(argv)

    shard_paths = find_shard_dbs(args.shards_dir)
    if not shard_paths:
        print(f"no {_SHARD_GLOB} files found in {args.shards_dir}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    stats = merge_shards(shard_paths, args.out)
    total = stats.pop("_total")
    total_deltas = stats.pop("_total_deltas")
    for name, n in stats.items():
        print(f"  {name}: {n} rows")
    # len(stats), not len(shard_paths): a shard that failed to ATTACH/read is skipped (see
    # merge_shards' resilience) and never gets a stats entry, so this reports how many shards
    # actually contributed rather than how many artifacts were merely found on disk.
    print(
        f"merged {len(stats)} shard(s), {total} expired_ids rows, {total_deltas} board_deltas "
        f"rows -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
