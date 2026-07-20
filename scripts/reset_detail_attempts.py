#!/usr/bin/env python3
"""Rescue stuck Tier-3 detail attempts: reset ``attempts = 0`` for every ``job_detail`` sidecar row
that never captured a snippet.

WHY: a row that hit ``RETRY_CAP`` (=3, see ``index/detail.py``) is abandoned by ``_eligible`` and
never re-fetched. Under the OLD (buggy) SmartRecruiters/Oracle detail parser, real postings whose
JD text sat one section away in the SAME payload were dropped as "no JD text" and burned their whole
retry budget -- so they're permanently stuck at ``attempts >= RETRY_CAP`` with an EMPTY snippet even
though the FIXED parser would now recover them. Zeroing ``attempts`` on exactly those rows (empty
snippet only) hands them a fresh retry budget so the next drain re-tries them with the fixed parser.

SAFE / IDEMPOTENT by construction:
  - Only rows with ``snippet IS NULL OR TRIM(snippet) = ''`` are touched -- a row that already has a
    recovered snippet (a SUCCESS) is never re-queued, so this can only ADD re-fetch work for
    genuinely-empty rows, never discard a recovered field.
  - Re-running finds nothing new to rescue: a row already at ``attempts = 0`` is a no-op UPDATE (the
    value is unchanged), and a row that gained a snippet since the last run is excluded by the
    snippet guard. The reported count is "rows matched", which converges to the stable empty-snippet
    set; nothing accumulates.

Usage:
  uv run python scripts/reset_detail_attempts.py --detail-db dist/index-detail.sqlite
  uv run python scripts/reset_detail_attempts.py --detail-db dist/index-detail.sqlite --dry-run

Reuses ``ergon_tracker.index.detail.open_detail`` for schema (no duplicated DDL); otherwise stdlib
only. ``open_detail`` is idempotent, so pointing this at a not-yet-existing sidecar simply creates
an empty one (0 rows rescued) rather than erroring.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.detail import open_detail  # noqa: E402

# The empty-snippet predicate: rows that never captured a JD snippet (the real-column signal for
# "not recovered yet" -- see index/detail.py::_tier3_rows, which uses the identical test on the
# index `jobs` table). A capped row matching this is one the OLD parser wrongly gave up on.
_EMPTY_SNIPPET = "snippet IS NULL OR TRIM(snippet) = ''"


def count_stuck(con: sqlite3.Connection) -> int:
    """Number of empty-snippet rows that would be reset (their ``attempts`` set to 0). Reported by
    ``--dry-run`` and used as the return count of :func:`reset_stuck_attempts`."""
    row = con.execute(
        f"SELECT COUNT(*) FROM job_detail WHERE ({_EMPTY_SNIPPET})"  # noqa: S608 - constant predicate
    ).fetchone()
    return int(row[0]) if row else 0


def reset_stuck_attempts(detail_db: str, *, dry_run: bool = False) -> int:
    """Reset ``attempts = 0`` for every empty-snippet ``job_detail`` row in ``detail_db``.

    Returns the number of rows matched by the empty-snippet predicate (the rescue candidate set).
    On ``dry_run`` the db is only read (no UPDATE, no commit). Idempotent: a second run matches the
    same converged empty-snippet set and changes nothing (each already at ``attempts = 0``)."""
    con = open_detail(detail_db)
    try:
        n = count_stuck(con)
        if not dry_run and n:
            con.execute(
                f"UPDATE job_detail SET attempts = 0 WHERE ({_EMPTY_SNIPPET})"  # noqa: S608
            )
            con.commit()
        return n
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--detail-db",
        type=Path,
        required=True,
        help="Path to the Tier-3 detail sidecar (index-detail.sqlite) to rescue",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many rows WOULD be reset without writing anything",
    )
    args = parser.parse_args(argv)

    n = reset_stuck_attempts(str(args.detail_db), dry_run=args.dry_run)
    verb = "would reset" if args.dry_run else "reset"
    print(f"{verb} attempts=0 on {n} empty-snippet row(s) in {args.detail_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
