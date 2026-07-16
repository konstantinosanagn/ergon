#!/usr/bin/env python3
"""Combine per-shard rich embedding sidecars into one ``index-vectors.sqlite``.

The sharded-embedding matrix runs 20 parallel jobs, each producing its own
``index-vectors-shard-N.sqlite`` covering a DISJOINT slice of the corpus (every posting id hashes to
exactly ONE shard, so no id appears in two shards -- mirrors the detail drain's shard-key design).
A separate ``merge`` job runs this script to union all 20 shard sidecars back into a single combined
``index-vectors.sqlite``, which is then published (with its manifest) as the rich vectors asset. This
is the vectors sibling of ``scripts/merge_detail_shards.py``.

Usage:
  uv run python scripts/merge_vectors_shards.py --shards-dir dist --out dist/index-vectors.sqlite

Reuses ``ergon_tracker.index.rich._ensure_schema`` for the ``job_vectors``/``meta`` schema (no
duplicated DDL) -- that's the only dependency on the ``ergon_tracker`` package; otherwise stdlib only
(sqlite3, argparse, glob via ``Path.glob``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.rich import _ensure_schema  # noqa: E402

_SHARD_GLOB = "index-vectors-shard-*.sqlite"


def find_shard_dbs(shards_dir: Path) -> list[Path]:
    """Shard files in ``shards_dir``, sorted for a deterministic merge order (so meta carry -- see
    ``merge_shards`` -- picks a stable "first" shard)."""
    return sorted(shards_dir.glob(_SHARD_GLOB))


def merge_shards(shard_paths: list[Path], out_path: Path) -> dict[str, int]:
    """Union every shard's ``job_vectors`` rows into ``out_path`` (schema ensured via
    ``_ensure_schema``). Returns ``{shard_filename: rows_merged, ..., "_total": total_rows_merged}``.

    Row union: shards are DISJOINT by id (each posting id hashes to exactly one shard), so this union
    never resolves a real conflict between two DIFFERENT shards' rows for the SAME id. The write uses
    ``INSERT OR REPLACE`` defensively anyway, so a re-run (e.g. after a retry) or an accidental overlap
    can never error or double-count -- it just overwrites with an identical row. Rows are streamed
    ATTACH+``INSERT ... SELECT`` (never ``fetchall``'d into Python), so peak memory is O(1) per shard.

    Meta carry: ``meta`` rows (schema_version, model, dim, quant, ...) are identical across shards by
    construction, so they're copied from the FIRST shard that has them via ``INSERT OR IGNORE`` --
    later shards can never clobber an already-carried key.

    Resilience: a shard whose file can't be ATTACHed / read (a truncated or empty artifact download)
    is skipped with a warning rather than aborting the whole merge, so one bad shard doesn't lose the
    other 19.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out_path))
    stats: dict[str, int] = {}
    total = 0
    try:
        _ensure_schema(con)  # create job_vectors + meta on the output db (no duplicated DDL)
        for shard_path in shard_paths:
            try:
                con.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
            except sqlite3.Error as exc:  # unreadable / not-a-db artifact -> skip, don't abort
                print(f"  WARNING: could not attach {shard_path.name}: {exc}", file=sys.stderr)
                continue
            try:
                before = con.total_changes
                # Disjoint by id, so OR REPLACE never resolves a real cross-shard conflict; it only
                # makes a re-run / accidental overlap idempotent instead of raising.
                con.execute(
                    "INSERT OR REPLACE INTO main.job_vectors (id, sig, scale, vec) "
                    "SELECT id, sig, scale, vec FROM shard.job_vectors"
                )
                n = con.total_changes - before
                # Carry meta from the FIRST shard that has each key (OR IGNORE => no clobber).
                con.execute(
                    "INSERT OR IGNORE INTO main.meta (key, value) SELECT key, value FROM shard.meta"
                )
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
        help="Combined output sidecar path (e.g. dist/index-vectors.sqlite)",
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
    print(f"merged {len(stats)} shard(s), {total} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
