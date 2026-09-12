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

Reuses ``ergon.index.detail.open_detail`` for schema (no duplicated DDL) and DERIVES the column
list from the table that call just ensured, so a column added to that module's ``DETAIL_SCHEMA``
is carried by the combine automatically instead of being silently dropped here (the v3
``remote``/``employment_type``/``level``/``sector`` columns were exactly that near-miss).
``ergon.index.detail`` is the only dependency on the ``ergon`` package; otherwise stdlib only
(sqlite3, argparse, glob via ``Path.glob``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon.index.detail import _UNKNOWN_SENTINELS, open_detail  # noqa: E402

_SHARD_GLOB = "index-detail-shard-*.sqlite"

# Recovered-metadata columns a fresher row must never BLANK OUT -- the same "a known value wins
# over unknown" rule ``index/detail.py::merge_detail_into_index`` applies when the sidecar lands in
# the core index. ``remote``/``employment_type``/``level`` spell unknown as the literal "unknown"
# sentinel (that module's ``_UNKNOWN_SENTINELS``, imported above); ``sector`` is plain nullable, so
# NULL is its unknown. Mirrors ``index/detail.py``'s v3 columns.
_PRESERVE_KNOWN_COLUMNS: frozenset[str] = frozenset(
    {"remote", "employment_type", "level", "sector"}
)


def find_shard_dbs(shards_dir: Path) -> list[Path]:
    """Shard files in ``shards_dir``, sorted for a deterministic merge order (see the meta-cursor
    note on ``merge_shards`` below -- "last shard wins" needs a stable "last")."""
    return sorted(shards_dir.glob(_SHARD_GLOB))


def job_detail_columns(con: sqlite3.Connection, schema: str = "main") -> tuple[str, ...]:
    """``job_detail``'s columns, in declaration order, read from the live db -- the combine's single
    source of truth for the column list (``main`` = the output db, whose schema ``open_detail`` has
    just ensured from ``index/detail.py::DETAIL_SCHEMA``). Also used on the ATTACHed ``shard`` to
    tolerate an OLDER shard artifact that predates a column."""
    return tuple(r[1] for r in con.execute(f"PRAGMA {schema}.table_info(job_detail)"))


def shard_select_list(columns: tuple[str, ...], shard_columns: set[str]) -> str:
    """``SELECT`` list for one shard: its own column where it has one, ``NULL`` where it does not --
    an older shard artifact predating a column reads as "unknown" for it instead of erroring."""
    return ", ".join(c if c in shard_columns else f"NULL AS {c}" for c in columns)


def _update_expr(col: str) -> str:
    """One column's ``ON CONFLICT DO UPDATE SET`` expression. Most columns take the incoming value
    outright; a ``_PRESERVE_KNOWN_COLUMNS`` one takes it only when it is KNOWN, so a re-fetch that
    failed to re-recover the field keeps what an earlier drain already found."""
    if col not in _PRESERVE_KNOWN_COLUMNS:
        return f"{col} = excluded.{col}"
    sentinel = _UNKNOWN_SENTINELS.get(col)
    if sentinel is None:  # nullable, no sentinel -- NULL is its "unknown"
        return f"{col} = COALESCE(excluded.{col}, job_detail.{col})"
    return (
        f"{col} = CASE WHEN excluded.{col} IS NOT NULL AND excluded.{col} <> '{sentinel}' "
        f"THEN excluded.{col} ELSE job_detail.{col} END"
    )


def merge_shards(
    shard_paths: list[Path], out_path: Path, base_path: Path | None = None
) -> dict[str, int]:
    """Union every shard's ``job_detail`` rows into ``out_path`` (schema ensured via
    ``open_detail``). Returns ``{shard_filename: rows_merged, ..., "_total": total_rows_merged}``.

    NON-DESTRUCTIVE COMBINE (``base_path``): when given, the output is SEEDED from the prior FULL
    combined sidecar before the shards are unioned on top, so a PARTIAL drain (only K<20 shards
    finished + uploaded) PRESERVES the un-finished shards' previously-drained rows instead of
    clobbering them. Without this seed the combine rebuilt from an empty db and the merge job
    published (with ``--clobber``) a sidecar holding only the finished shards' ~K/20 of the rows --
    permanently deleting the missing shards' list-only JD (the 2026-07-26 ``with_jd`` 85%->47%
    regression: SmartRecruiters/Workday megahost shards timing out gutted the sidecar every drain).
    The prefer-freshest, ``fetched_at``-keyed UPSERT below makes the seed safe: a finished shard's
    freshly-fetched row still refreshes its seeded copy; only rows whose shard did NOT finish are
    left as the base carried them. ``None`` (or an absent path) -> the legacy fresh-empty combine.

    Row union: as of the ``_prune_sidecar_to_shard`` fix in ``index/detail.py``, each shard's
    OUTPUT sidecar is scoped to contain ONLY that shard's own rows, so shard candidate sets are
    DISJOINT by construction (each posting's politeness bucket hashes to exactly one shard) and
    this union never resolves a real conflict between two DIFFERENT shards' rows for the SAME id.

    Belt-and-suspenders anyway: the write below is a prefer-freshest UPSERT, not a blind
    ``INSERT OR REPLACE`` -- an incoming row only overwrites an existing one when the incoming
    ``fetched_at`` is non-NULL and is not older than what's already there (``existing IS NULL OR
    incoming >= existing``). This is deliberately order-independent (merging shard A then B gives
    the same result as B then A) and guards correctness even if the disjointness invariant above
    is ever violated by a future change -- a later shard's STALE carried-forward copy of a row
    (``fetched_at`` NULL, or an older ``fetched_at``) can never clobber an earlier shard's FRESHLY
    -fetched copy of the same id. Also safe to re-run the merge (e.g. after a retry) without
    double-counting or erroring on a re-processed shard.

    Recovered-metadata columns (``_PRESERVE_KNOWN_COLUMNS``): for these the row-level upsert is
    additionally column-level NON-CLOBBERING -- an incoming UNKNOWN (NULL, or the literal "unknown"
    sentinel the enum-backed ones use) leaves the existing KNOWN value in place, while an incoming
    known value fills an existing unknown. Without this, a row re-fetched by this drain whose
    provider happened not to expose e.g. ``remote`` this time would blank out the value an earlier
    drain recovered -- every drain, for as long as the field keeps not coming back. The freshness
    gate still applies: a value only lands from a row that passes the ``fetched_at`` check below.

    Older shard artifacts: the per-shard SELECT is built from the OUTPUT table's columns, with a
    NULL stand-in for any the shard's own ``job_detail`` lacks, so a v2 shard (no ``remote``/
    ``employment_type``/``level``/``sector``) merges as "unknown for those" instead of failing --
    and, by the non-clobber rule above, cannot erase a v3 shard's or the base's known values.

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
    # Seed the combine from the prior full sidecar so an incomplete shard set never DROPS rows (see
    # NON-DESTRUCTIVE COMBINE above). Copied BEFORE open_detail so open_detail just re-ensures the
    # (identical) schema on the seeded file. A missing/None base -> fresh-empty combine (legacy).
    if base_path is not None and Path(base_path).exists():
        import shutil

        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base_path, out_path)
    con = open_detail(str(out_path))
    stats: dict[str, int] = {}
    total = 0
    columns = job_detail_columns(con)  # derived, not hardcoded -- see the module docstring
    cols = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    update_set = ", ".join(_update_expr(c) for c in columns if c != "id")
    # NOTE: SQLite's upsert-clause grammar does not accept an `INSERT ... SELECT ... ON CONFLICT`
    # form (the parser treats the `ON` as a join constraint on the FROM'd table and chokes on the
    # following `DO`) -- only the VALUES form is accepted. So each shard's rows are pulled into
    # Python and re-inserted via a parameterized, per-row VALUES upsert (mirrors the exact pattern
    # `index/detail.py::_record_success` already uses). Bounded by one shard's own row count.
    upsert_sql = (
        f"INSERT INTO job_detail ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set} "
        "WHERE excluded.fetched_at IS NOT NULL "
        "AND (job_detail.fetched_at IS NULL OR excluded.fetched_at >= job_detail.fetched_at)"
    )
    try:
        for shard_path in shard_paths:
            # Ensure the shard file itself has the schema (defensive; a truncated/empty artifact
            # download would otherwise fail the ATTACH+SELECT below with a confusing error).
            open_detail(str(shard_path)).close()
            con.execute("ATTACH DATABASE ? AS shard", (str(shard_path),))
            try:
                # A pre-v3 shard artifact lacks the newer columns -> select NULL ("unknown") for
                # them rather than erroring on an unknown identifier.
                select_list = shard_select_list(columns, set(job_detail_columns(con, "shard")))
                rows = con.execute(f"SELECT {select_list} FROM shard.job_detail").fetchall()
                before = con.total_changes
                con.executemany(upsert_sql, rows)
                n = con.total_changes - before
                stats[shard_path.name] = n
                total += n
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
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Prior full combined sidecar to SEED the output from (non-destructive combine): a "
        "partial shard set unions onto this instead of clobbering it. Absent/missing -> fresh combine.",
    )
    args = parser.parse_args(argv)

    shard_paths = find_shard_dbs(args.shards_dir)
    if not shard_paths:
        print(f"no {_SHARD_GLOB} files found in {args.shards_dir}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    stats = merge_shards(shard_paths, args.out, base_path=args.base)
    total = stats.pop("_total")
    for name, n in stats.items():
        print(f"  {name}: {n} rows")
    print(f"merged {len(shard_paths)} shard(s), {total} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
