#!/usr/bin/env python3
"""Coverage report for the Tier-3 detail-fetcher's recovered fields, read against a LOCAL index db.

Prints, per Tier-3 source, the POPULATED share (non-NULL/non-empty count over total row count, as
a percentage) for `snippet`, `salary_min`, `years_min`, `degree_min` -- so the real lift a
`--detail` CI run bought can be read straight off the index, before/after. Follows the
populated-fill discipline used elsewhere in this project: a column merely being *present* in the
schema proves nothing -- only a non-NULL, non-empty VALUE counts as coverage.

Usage:
  python scripts/measure_detail_coverage.py <index.sqlite> [--sources smartrecruiters,workday]

Read-only: opens the index via sqlite3's URI `mode=ro` and never writes to it. Stdlib only
(sqlite3, argparse) -- no runtime dependency on the `ergon_tracker` package.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

# The Tier-3 (detail-fetcher) source lineup per the implementation plan (oracle/icims/workday
# proving sources, smartrecruiters shipped first) -- overridable via --sources.
DEFAULT_SOURCES: tuple[str, ...] = ("oracle", "icims", "workday", "smartrecruiters")

# (index column, needs a TRIM(...) != '' check). TEXT columns (`snippet`, `degree_min`) can hold
# an empty string that isn't "populated"; numeric columns (`salary_min`, `years_min`) can't.
_FIELDS: tuple[tuple[str, bool], ...] = (
    ("snippet", True),
    ("salary_min", False),
    ("years_min", False),
    ("degree_min", True),
)


@dataclass(frozen=True)
class SourceCoverage:
    source: str
    total: int
    populated: dict[str, int] = field(default_factory=dict)

    def pct(self, field_name: str) -> float:
        if self.total == 0:
            return 0.0
        return 100.0 * self.populated.get(field_name, 0) / self.total


def _populated_expr(column: str, needs_trim: bool) -> str:
    if needs_trim:
        return f"SUM(CASE WHEN {column} IS NOT NULL AND TRIM({column}) != '' THEN 1 ELSE 0 END)"
    return f"SUM(CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END)"


def measure(con: sqlite3.Connection, sources: Sequence[str]) -> list[SourceCoverage]:
    """Compute per-source populated coverage for `_FIELDS`. A source with 0 rows reports 0 for
    every field (never divides by zero)."""
    results: list[SourceCoverage] = []
    for source in sources:
        total_row = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE source = ?", (source,)
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        if total == 0:
            results.append(SourceCoverage(source, 0, {name: 0 for name, _ in _FIELDS}))
            continue
        exprs = ", ".join(_populated_expr(col, trim) for col, trim in _FIELDS)
        row = con.execute(f"SELECT {exprs} FROM jobs WHERE source = ?", (source,)).fetchone()
        populated = {name: int(row[i] or 0) for i, (name, _) in enumerate(_FIELDS)}
        results.append(SourceCoverage(source, total, populated))
    return results


def format_report(rows: Sequence[SourceCoverage]) -> str:
    col_width = 20
    header = f"{'source':<16}{'total':>8}  " + "  ".join(
        f"{name:>{col_width}}" for name, _ in _FIELDS
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        cells = []
        for name, _ in _FIELDS:
            n = r.populated.get(name, 0)
            cells.append(f"{n}/{r.total} ({r.pct(name):.1f}%)".rjust(col_width))
        lines.append(f"{r.source:<16}{r.total:>8}  " + "  ".join(cells))
    return "\n".join(lines)


def _parse_sources(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_SOURCES
    parsed = tuple(s.strip() for s in raw.split(",") if s.strip())
    return parsed or DEFAULT_SOURCES


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index_path", help="Path to a local index.sqlite (read-only)")
    parser.add_argument(
        "--sources",
        default=None,
        help=f"Comma-separated Tier-3 sources (default: {','.join(DEFAULT_SOURCES)})",
    )
    args = parser.parse_args(argv)
    sources = _parse_sources(args.sources)

    con = sqlite3.connect(f"file:{args.index_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    try:
        rows = measure(con, sources)
    finally:
        con.close()

    print(format_report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
