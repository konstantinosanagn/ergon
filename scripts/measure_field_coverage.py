#!/usr/bin/env python3
"""Operator tool (NOT a pytest test): measure structured-field populated-coverage in the local
cached index, overall and per-source, for the providers touched by the structured-field-recovery
Stage-1 plan.

"Populated" means the field has a real value, not merely a present-but-empty column (e.g.
recruitee's ``salary`` object is present on ~100% of rows but ``min``/``max`` populated on far
fewer) -- the same distinction the plan's live gates enforce.

Usage:
    uv run python scripts/measure_field_coverage.py

Reads ``~/.cache/ergon-tracker/index.sqlite`` (the locally built index). If it doesn't exist yet
(e.g. a fresh checkout that hasn't run ``scripts/build_index.py``), prints a friendly message and
exits 0 -- this is an operator convenience, not a CI gate.
"""

from __future__ import annotations

import os
import sqlite3
import sys

INDEX_PATH = os.path.expanduser("~/.cache/ergon-tracker/index.sqlite")

# Providers Tasks 2-7 of the structured-field-recovery plan mapped structured fields for.
_TOUCHED_SOURCES = (
    "smartrecruiters",
    "jazzhr",
    "workable",
    "join",
    "breezy",
    "personio",
    "recruitee",
)

_ACTIVE = "expired_at IS NULL"

_FIELD_WHERE = {
    "level": "level != 'unknown' AND level IS NOT NULL",
    "salary": "salary_min IS NOT NULL OR salary_max IS NOT NULL",
    "degree": "degree_min IS NOT NULL",
    "years": "years_min IS NOT NULL OR years_max IS NOT NULL",
}


def _coverage(conn: sqlite3.Connection, where: str, source: str | None = None) -> tuple[int, int]:
    clause = f"{_ACTIVE} AND ({where})"
    total_clause = _ACTIVE
    params: tuple[str, ...] = ()
    if source is not None:
        clause += " AND source = ?"
        total_clause += " AND source = ?"
        params = (source,)
    n = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {clause}", params).fetchone()[0]
    tot = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {total_clause}", params).fetchone()[0]
    return n, tot


def main() -> int:
    if not os.path.exists(INDEX_PATH):
        print(f"No local index found at {INDEX_PATH} -- build one with scripts/build_index.py")
        return 0

    conn = sqlite3.connect(INDEX_PATH)
    try:
        print(f"index: {INDEX_PATH}\n")

        print("overall populated-coverage (active postings):")
        for label, where in _FIELD_WHERE.items():
            n, tot = _coverage(conn, where)
            pct = n / tot if tot else 0.0
            print(f"  {label:8} {n:>9,}/{tot:,} = {pct:.1%}")

        print("\nper-source level/salary/degree/years coverage (structured-field-recovery Stage 1):")
        for src in _TOUCHED_SOURCES:
            _, src_total = _coverage(conn, "1=1", src)
            if src_total == 0:
                print(f"  {src:16} (no postings in local index)")
                continue
            parts = []
            for label, where in _FIELD_WHERE.items():
                n, tot = _coverage(conn, where, src)
                pct = n / tot if tot else 0.0
                parts.append(f"{label}={n:,}/{tot:,} ({pct:.0%})")
            print(f"  {src:16} " + "  ".join(parts))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
