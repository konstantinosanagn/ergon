#!/usr/bin/env python3
"""Combine per-shard Tier-3 detail sidecars into one ``index-detail.sqlite``.

The drain matrix (``.github/workflows/drain-detail.yml``) runs 20 parallel
``build_index.py --detail-shard-only`` jobs, each producing its own
``index-detail-shard-N.sqlite`` covering a DISJOINT slice of Tier-3 candidates (every posting's
politeness bucket hashes to exactly ONE shard -- see ``index/detail.py``'s shard-key design). The
drain workflow's separate ``merge`` job runs this script to union all 20 shard sidecars back into
a single combined ``index-detail.sqlite``, which is then published alongside its manifest. The
next daily ``build-index.yml`` run downloads that combined sidecar as its carry-forward and merges
its recovered fields into the core index via the EXISTING (unsharded) ``build_and_publish_detail``
path -- this script never touches the core index itself.

Usage:
  uv run python scripts/merge_detail_shards.py --shards-dir dist --out dist/index-detail.sqlite

Reuses ``ergon_tracker.index.detail.open_detail`` for schema (no duplicated DDL) -- that's the
only dependency on the ``ergon_tracker`` package; otherwise stdlib only (sqlite3, argparse, glob
via ``Path.glob``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.detail import open_detail  # noqa: E402

_SHARD_GLOB = "index-detail-shard-*.sqlite"

_JOB_DETAIL_COLUMNS: tuple[str, ...] = (
    "id",
    "sig",
    "fetched_at",
    "attempts",
    "snippet",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_interval",
    "years_min",
    "years_max",
    "degree_min",
    "degree_required",
    "sponsorship_offered",
)


def find_shard_dbs(shards_dir: Path) -> list[Path]:
    """Shard files in ``shards_dir``, sorted for a deterministic merge order (see the meta-cursor
    note on ``merge_shards`` below -- "last shard wins" needs a stable "last")."""
    return sorted(shards_dir.glob(_SHARD_GLOB))


def merge_shards(shard_paths: list[Path], out_path: Path) -> dict[str, int]:
    """Union every shard's ``job_detail`` rows into ``out_path`` (schema ensured via
    ``open_detail``). Returns ``{shard_filename: rows_merged, ..., "_total": total_rows_merged}``.

    Row union: shard candidate sets are DISJOINT by construction (each posting's politeness bucket
    hashes to exactly one shard), so ``INSERT OR REPLACE`` never actually resolves a real conflict
    between two DIFFERENT shards' rows -- it's just the simplest idempotent write, safe to re-run
    the merge (e.g. after a retry) without double-counting or erroring on a re-processed shard.

    Meta-cursor handling: each shard sidecar carries its OWN rotating ``detail_cursor`` (see
    ``index/detail.py::_select_window``), scoped to that shard's own candidate subset -- these
    per-shard cursors are NOT individually meaningful once combined. The combined sidecar is
    consumed by the daily (UNsharded) ``build_and_publish_detail`` reconcile, which computes its
    own candidate list over the whole backlog and rotates via a cursor of its own. So this merge
    does the simplest sensible thing: whichever shard is processed LAST (sorted path order) wins
    for ``detail_cursor`` in the combined db's ``meta`` table. Any value is fine here -- nothing
    correctness-relevant depends on it, since the unsharded reconcile just starts its own rotation
    from wherever that lands. ``schema_version`` is left as whatever ``open_detail`` already
    ensured on ``out_path`` (identical across shards by construction, via ``DETAIL_SCHEMA_VERSION``).
    """
    con = open_detail(str(out_path))
    stats: dict[str, int] = {}
    total = 0
    cols = ", ".join(_JOB_DETAIL_COLUMNS)
    try:
        for shard_path in shard_paths:
            # Ensure the shard file itself has the schema (defensive; a truncated/empty artifact
            # download would otherwise fail the ATTACH+SELECT below with a confusing error).
            open_detail(str(shard_path)).close()
            con.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
            try:
                cur = con.execute(
                    f"INSERT OR REPLACE INTO job_detail ({cols}) "
                    f"SELECT {cols} FROM shard.job_detail"
                )
                n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                stats[shard_path.name] = n
                total += n
                cur.close()
                # Best-effort meta carry: last-shard-wins (see docstring above).
                meta_cur = con.execute("SELECT value FROM shard.meta WHERE key = 'detail_cursor'")
                cursor_row = meta_cur.fetchone()
                meta_cur.close()
                if cursor_row is not None:
                    con.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES('detail_cursor', ?)",
                        (cursor_row[0],),
                    )
                con.commit()  # release the shard-touching transaction before DETACH below
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
        help="Combined output sidecar path (e.g. dist/index-detail.sqlite)",
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
    print(f"merged {len(shard_paths)} shard(s), {total} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
