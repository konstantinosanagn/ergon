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
not resolve. Otherwise stdlib only (sqlite3, argparse, glob via ``Path.glob``) for the union logic
-- the one exception is the expiry-rate-monitor's drift tripwire, which imports its canonical
formula from ``ergon_tracker.index.freshness`` (see the import block below for why that resolves).

EXPIRY-RATE MONITOR (drift tripwire): this merge is also where each shard's own ``source_stats``
counts (see ``freshness_sweep.py``'s ``_SOURCE_STATS_SCHEMA``) get SUMMED into one grand total per
source and published into the combined sidecar's ``source_stats`` table -- inspectable directly
(``SELECT * FROM source_stats``). ``ergon_tracker.index.freshness.check_expiry_alarms`` is then
evaluated on those merged totals (never a single shard's slice -- see that function's docstring for
why) to WARN when a source's expiry rate spikes, e.g. a body-marker soft-404 source (adp/taleo/
taleobe) whose "confirmed dead" marker match starts drifting onto live postings. Observability
only: this never changes an expiry decision, never touches ``expired_ids``/``board_deltas``, and
never fails the merge or the workflow.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# The expiry-rate-monitor's drift tripwire (``check_expiry_alarms``/``source_expiry_rate``) is the
# ONE piece of this script that needs real logic beyond "union some rows" -- rather than duplicate
# it here (and risk the two formulas drifting apart), import the canonical implementation from
# ``ergon_tracker.index.freshness``. This mirrors ``scripts/freshness_sweep.py``'s own
# ``sys.path.insert(0, str(ROOT / "src"))`` trick so the import resolves regardless of HOW this
# file is invoked (``python scripts/merge_freshness_shards.py`` from the repo root, where
# ``sys.path[0]`` is ``scripts/`` itself, not the root) -- everything else in this module stays
# stdlib-only, as before.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.freshness import check_expiry_alarms  # noqa: E402

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

# Expiry-rate-monitor addition -- MUST stay byte-for-byte identical (modulo IF NOT EXISTS) to
# `scripts/freshness_sweep.py`'s `_SOURCE_STATS_SCHEMA`. UNLIKE `expired_ids`/`board_deltas` (which
# are unioned/deduped by natural key), a source's counts must be SUMMED across shards -- each shard
# only ever covers a disjoint slice of a source's boards, so its counts are a partial total, not a
# duplicate of another shard's. The merged table therefore carries exactly one row per source (the
# grand total across every shard merged), which is what `check_expiry_alarms` needs: a per-shard
# rate is not a trustworthy drift signal (see that function's docstring).
_SOURCE_STATS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS source_stats"
    "(source TEXT PRIMARY KEY, checked INTEGER NOT NULL, candidates INTEGER NOT NULL, "
    "departed INTEGER NOT NULL, expired INTEGER NOT NULL, confirmed_alive INTEGER NOT NULL, "
    "unconfirmed INTEGER NOT NULL, errored INTEGER NOT NULL)"
)

_STATS_KEYS: tuple[str, ...] = (
    "checked",
    "candidates",
    "departed",
    "expired",
    "confirmed_alive",
    "unconfirmed",
    "errored",
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


def _read_shard_stats(con: sqlite3.Connection, shard_name: str) -> dict[str, dict[str, int]]:
    """Read one ATTACHed shard's own ``source_stats`` rows (see ``freshness_sweep.py``'s
    ``_SOURCE_STATS_SCHEMA``), returning ``{source: {counts}}``. A shard predating this table
    (legacy shard) or an otherwise-unreadable table returns ``{}`` with the same per-table warning
    ``_union_table`` prints -- never aborts the merge, mirroring that function's resilience."""
    try:
        rows = con.execute(
            "SELECT source, checked, candidates, departed, expired, confirmed_alive, "
            "unconfirmed, errored FROM shard.source_stats"
        ).fetchall()
    except sqlite3.Error as exc:  # missing/truncated table inside the shard -> {}, don't abort
        print(f"  WARNING: could not read source_stats in {shard_name}: {exc}", file=sys.stderr)
        return {}
    return {r[0]: dict(zip(_STATS_KEYS, r[1:], strict=True)) for r in rows}


def merge_shards(shard_paths: list[Path], out_path: Path) -> dict[str, int | list[str]]:
    """Union every shard's ``expired_ids`` AND ``board_deltas`` rows into ``out_path`` (both schemas
    ensured via the pinned ``_SIDECAR_SCHEMA`` / ``_BOARD_DELTAS_SCHEMA`` DDL), AND SUM every
    shard's ``source_stats`` counts into one grand-total row per source (``_SOURCE_STATS_SCHEMA``).
    Returns ``{shard_filename: expired_rows_merged, ..., "_total": total_expired,
    "_total_deltas": total_delta_rows, "_expiry_alarms": [source, ...]}`` -- per-shard values report
    the ``expired_ids`` count (the long-standing contract); the delta total and the fired-alarm
    source list are reported separately so the ``expired_ids`` numbers callers already parse are
    unchanged.

    Row union (``expired_ids``/``board_deltas``): shards partition boards disjointly by
    host/rate-bucket (see ``freshness_shard.shard_boards``), so two DIFFERENT shards should never
    confirm-depart the SAME posting id NOR emit a delta for the SAME (source, board_token) -- but
    even if they did, any duplicate row is semantically IDENTICAL. So both writes are
    ``INSERT OR IGNORE``: the first shard (in sorted path order) to claim a key wins, later
    duplicates are silently dropped rather than erroring -- order-INSENSITIVE for correctness, and
    safe to re-run the merge (e.g. after a retry) without erroring on a re-processed shard.

    ``source_stats`` is different: a shard's counts for a source are a PARTIAL total (that shard's
    slice of that source's boards), not a duplicate of another shard's -- so instead of
    ``INSERT OR IGNORE``, every shard's counts are SUMMED in Python (``combined``) and the final
    per-source totals fully REPLACE ``main.source_stats`` at the end (``DELETE`` then insert) --
    idempotent on a merge re-run with the same shard set, exactly like the other two tables, but via
    recompute-from-scratch rather than key-level dedup (summed counts can't be deduped by key the
    way a natural-keyed row can).

    DRIFT TRIPWIRE: after every shard's counts are summed, ``check_expiry_alarms`` (the canonical
    ``ergon_tracker.index.freshness`` implementation -- see the WHY at this module's top) is
    evaluated on the MERGED, cross-shard totals -- never a single shard's slice, which is not a
    trustworthy per-source rate on its own. This ONLY logs a WARNING for any source whose expiry
    rate spiked past ``ERGON_FRESHNESS_EXPIRY_ALARM``; it never touches ``expired_ids``,
    ``board_deltas``, or any expiry decision -- observability only.

    Rows are streamed via ATTACH + ``INSERT ... SELECT`` (never ``fetchall``'d into Python) for
    ``expired_ids``/``board_deltas``, so peak memory is O(1) per shard regardless of how many
    ids/deltas a shard produced; ``source_stats`` is necessarily read into Python to sum (one row
    per source per shard -- a small, bounded fan-in given ~30 known sources).

    Resilience: a shard whose file can't be ATTACHed (a truncated or empty artifact download) is
    skipped with a warning rather than aborting the whole merge; a shard missing any ONE of the
    three tables still contributes the tables it does have (each table's merge is guarded
    independently). One bad shard never loses the others (mirrors ``merge_vectors_shards.py``'s
    per-shard resilience).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(out_path))
    stats: dict[str, int | list[str]] = {}
    total = 0
    total_deltas = 0
    combined_stats: dict[str, dict[str, int]] = {}
    try:
        con.execute(_SIDECAR_SCHEMA)
        con.execute(_BOARD_DELTAS_SCHEMA)
        con.execute(_SOURCE_STATS_SCHEMA)
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
                for source, counts in _read_shard_stats(con, shard_path.name).items():
                    running = combined_stats.setdefault(source, dict.fromkeys(_STATS_KEYS, 0))
                    for key in _STATS_KEYS:
                        running[key] += counts.get(key, 0)
                stats[shard_path.name] = n
                total += n
                total_deltas += nd
            finally:
                # Each _union_table already committed (or rolled back) its own transaction, so
                # there is no open transaction here -- DETACH is safe.
                con.execute("DETACH DATABASE shard")
        # Full recompute-from-scratch (not an incremental add) -- see docstring -- so a merge
        # re-run over the SAME shard set is idempotent rather than double-counting.
        con.execute("DELETE FROM main.source_stats")
        con.executemany(
            "INSERT INTO main.source_stats(source, checked, candidates, departed, expired, "
            "confirmed_alive, unconfirmed, errored) VALUES (?,?,?,?,?,?,?,?)",
            [
                (source, *(counts[key] for key in _STATS_KEYS))
                for source, counts in sorted(combined_stats.items())
            ],
        )
        con.commit()
    finally:
        con.close()
    stats["_total"] = total
    stats["_total_deltas"] = total_deltas
    stats["_expiry_alarms"] = check_expiry_alarms(combined_stats)
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
    fired = stats.pop("_expiry_alarms")
    assert isinstance(fired, list)  # narrows int | list[str] -> list[str], for mypy and safety
    for name, n in stats.items():
        print(f"  {name}: {n} rows")
    # len(stats), not len(shard_paths): a shard that failed to ATTACH/read is skipped (see
    # merge_shards' resilience) and never gets a stats entry, so this reports how many shards
    # actually contributed rather than how many artifacts were merely found on disk.
    print(
        f"merged {len(stats)} shard(s), {total} expired_ids rows, {total_deltas} board_deltas "
        f"rows -> {args.out}"
    )
    # check_expiry_alarms already logged a WARNING per fired source (see merge_shards); this is
    # just a visible one-line CLI summary, never a build/exit-code failure -- the tripwire is
    # strictly observability-only (see this module's top-of-file WHY note).
    if fired:
        print(f"  EXPIRY RATE ALARM: {len(fired)} source(s) spiked -- {', '.join(fired)}")
    # Emit the tripwire as a small machine-readable signal alongside the sidecar so
    # freshness-sweep.yml's notify step can alert on it (scripts/notify_ops.py --from-json). Kept
    # strictly additive + observability-only: writing it never changes an expiry decision, and a
    # write hiccup here must never fail the merge -- best-effort, mirroring the tripwire itself.
    try:
        alarm_path = args.out.parent / "expiry_alarm.json"
        alarm_path.write_text(json.dumps({"fired": fired, "total_expired": total}, indent=1))
    except OSError as exc:  # best-effort observability sidecar -- never fail the merge on it
        print(f"  WARNING: could not write expiry_alarm.json: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
