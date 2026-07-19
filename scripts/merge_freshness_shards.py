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


def find_shard_dbs(shards_dir: Path) -> list[Path]:
    """Shard files in ``shards_dir``, sorted for a deterministic, reproducible merge order."""
    return sorted(shards_dir.glob(_SHARD_GLOB))


def merge_shards(shard_paths: list[Path], out_path: Path) -> dict[str, int]:
    """Union every shard's ``expired_ids`` rows into ``out_path`` (schema ensured via the pinned
    ``_SIDECAR_SCHEMA`` DDL). Returns ``{shard_filename: rows_merged, ..., "_total": total_rows}``.

    Row union: shards partition boards disjointly by host/rate-bucket (see
    ``freshness_shard.shard_boards``), so two DIFFERENT shards should never confirm-depart the
    SAME posting id -- but even if they did, the task's semantics are that any duplicate row is
    IDENTICAL (an id either departed its board or it didn't; ``expired_at``/``reason`` carry no
    per-shard-specific meaning worth reconciling). So the write is ``INSERT OR IGNORE``: the first
    shard (in sorted path order) to claim an id wins, later duplicates are silently dropped rather
    than erroring -- deliberately order-INSENSITIVE for correctness (which shard "wins" on a
    coincidental duplicate is immaterial since the rows are semantically identical), and safe to
    re-run the merge (e.g. after a retry) without erroring on a re-processed shard.

    Rows are streamed via ATTACH + ``INSERT ... SELECT`` (never ``fetchall``'d into Python), so
    peak memory is O(1) per shard regardless of how many ids a shard confirmed departed.

    Resilience: a shard whose file can't be ATTACHed / read (a truncated or empty artifact
    download, or one missing the ``expired_ids`` table) is skipped with a warning rather than
    aborting the whole merge, so one bad shard doesn't lose the other 19 (mirrors
    ``merge_vectors_shards.py``'s per-shard resilience).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out_path))
    stats: dict[str, int] = {}
    total = 0
    try:
        con.execute(_SIDECAR_SCHEMA)
        for shard_path in shard_paths:
            try:
                con.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
            except sqlite3.Error as exc:  # unreadable / not-a-db artifact -> skip, don't abort
                print(f"  WARNING: could not attach {shard_path.name}: {exc}", file=sys.stderr)
                continue
            try:
                before = con.total_changes
                # Disjoint by construction (see docstring), so OR IGNORE never silently drops a
                # real cross-shard disagreement -- it only makes a re-run / coincidental overlap
                # idempotent instead of raising a PRIMARY KEY conflict.
                con.execute(
                    "INSERT OR IGNORE INTO main.expired_ids (id, expired_at, reason) "
                    "SELECT id, expired_at, reason FROM shard.expired_ids"
                )
                n = con.total_changes - before
                stats[shard_path.name] = n
                total += n
                con.commit()  # release the shard-touching transaction before DETACH
            except sqlite3.Error as exc:  # truncated/missing table inside the shard -> skip it
                print(f"  WARNING: could not read {shard_path.name}: {exc}", file=sys.stderr)
                con.rollback()
            finally:
                con.execute("DETACH DATABASE shard")
        con.commit()
    finally:
        con.close()
    stats["_total"] = total
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
    for name, n in stats.items():
        print(f"  {name}: {n} rows")
    # len(stats), not len(shard_paths): a shard that failed to ATTACH/read is skipped (see
    # merge_shards' resilience) and never gets a stats entry, so this reports how many shards
    # actually contributed rather than how many artifacts were merely found on disk.
    print(f"merged {len(stats)} shard(s), {total} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
