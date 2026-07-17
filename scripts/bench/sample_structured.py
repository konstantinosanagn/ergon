"""Structured-only index sample CLI (Filter Benchmark v2, Task 4).

Complements the JD crawl (``crawl_corpus.py``, Task 3) with a cheap-to-scale supplement: rows
sampled directly from the prebuilt index for the fields that need NO job-description text --
level, geo (country/city), sector, employment_type, remote, recency/posted_at. The index
(``src/ergon_tracker/index/schema.sql``) never stores the full JD body (only a short ``snippet``,
kept here for context but never fed into ``description_text``), so this corpus can scale to
10k+ rows for free by reading straight off the ``jobs`` table -- unlike the JD crawl, which is
bounded by live network fetches.

    python -m scripts.bench.sample_structured --out bench/corpus_structured.jsonl --total 10000 --floor 200

``--total``/``--floor`` are ROW budgets, same convention as ``crawl_corpus``: ``--total`` bounds
the number of rows written overall, ``--floor`` bounds the minimum rows guaranteed per source
(capped at that source's realized availability, via :func:`scripts.bench.strata.allocate`).
Sampling per source is deterministic (``ORDER BY id LIMIT n``), so reruns against an unchanged db
draw the same rows. Realized per-source counts are always printed -- nothing is silently
truncated.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from ergon_tracker.index.cache import IndexCache

from .schema import corpus_row, write_jsonl
from .strata import allocate

__all__ = [
    "STRUCTURED_COLUMNS",
    "default_db_path",
    "source_counts",
    "stratified_sql_ids",
    "sample_source",
    "sample_structured",
    "main",
]

ROOT = Path(__file__).resolve().parents[2]

# Columns read straight off the prebuilt index for the no-JD fields this corpus covers (level,
# geo, sector, employment_type, remote, recency) -- see index/schema.sql. Deliberately excludes
# any JD-body column: the index never stores full description text. ``snippet`` is kept for
# context/debugging under its own key -- never mapped onto ``description_text``, so nothing
# downstream (predict.py's reconstruction) can mistake it for a real JD.
STRUCTURED_COLUMNS: tuple[str, ...] = (
    "id",
    "source",
    "company",
    "title",
    "location",
    "city",
    "country",
    "remote",
    "level",
    "employment_type",
    "sector",
    "posted_at",
    "snippet",
)


def default_db_path() -> Path:
    """The cached published index if present, else the repo-local ``dist/index.sqlite``."""
    cached = IndexCache().db_path
    if cached.exists():
        return cached
    return ROOT / "dist" / "index.sqlite"


def source_counts(con: sqlite3.Connection) -> dict[str, int]:
    """Row count per ``source`` in the ``jobs`` table."""
    rows = con.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source").fetchall()
    return {str(source): int(count) for source, count in rows}


def stratified_sql_ids(counts: dict[str, int], total: int, floor: int) -> dict[str, int]:
    """Provider -> row count to draw, stratified across ``counts`` via :func:`strata.allocate`.

    Pure helper (no sqlite I/O), unit-tested against a synthetic ``counts`` dict: every provider
    present in ``counts`` with available>0 rows gets at least ``min(available, floor)``, the
    remainder is spread proportionally, and the sum drawn never exceeds ``total`` nor any single
    provider's real availability. Named for what its output does: it sizes the per-source
    ``ORDER BY id LIMIT n`` draw in :func:`sample_source`.
    """
    return allocate(counts, total, floor)


def _row_to_corpus_row(record: dict[str, Any]) -> dict[str, Any]:
    """One ``jobs`` row (column name -> value) -> a ``corpus_row``.

    NO ``description_text`` -- these rows exist for the no-JD fields only. Carries the
    provider-STATED ``employment_type``/``remote`` straight off the index columns (same
    convention as ``crawl_corpus.row_from_job``: neither is ever inferred by the extractor
    pipeline, so the corpus must record exactly what the ATS/index gave us), plus the other
    no-JD structured fields this corpus supplements: ``level``, ``sector``, ``country``,
    ``city``, ``posted_at``. ``id`` is ``"<source>:<index_id>"`` so downstream provider-matrix
    grouping can recover ``source`` from the id prefix even if it's dropped along the way.
    """
    source = str(record["source"])
    return corpus_row(
        id=f"{source}:{record['id']}",
        source=source,
        company=record.get("company") or "",
        title=record.get("title") or "",
        location_raw=record.get("location") or "",
        employment_type=record.get("employment_type") or "unknown",
        remote=record.get("remote") or "unknown",
        level=record.get("level") or "unknown",
        sector=record.get("sector"),
        country=record.get("country"),
        city=record.get("city"),
        posted_at=record.get("posted_at"),
        snippet=record.get("snippet") or "",
    )


def sample_source(con: sqlite3.Connection, source: str, n: int) -> list[dict[str, Any]]:
    """Deterministically draw up to ``n`` rows for ``source`` (``ORDER BY id`` so reruns against
    an unchanged db are stable), mapped through :func:`_row_to_corpus_row`.

    Returns fewer than ``n`` rows only if ``source`` itself has fewer than ``n`` rows available --
    never pads or duplicates to reach ``n``.
    """
    if n <= 0:
        return []
    cols = ", ".join(STRUCTURED_COLUMNS)
    query = f"SELECT {cols} FROM jobs WHERE source = ? ORDER BY id LIMIT ?"
    cur = con.execute(query, (source, n))
    assert cur.description is not None
    fields = [d[0] for d in cur.description]
    return [_row_to_corpus_row(dict(zip(fields, row, strict=True))) for row in cur.fetchall()]


def sample_structured(
    db_path: Path, total: int, floor: int
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """Sample ``total`` structured-only corpus rows from ``db_path``, stratified per source.

    Returns ``(rows, available, realized)``: ``available`` is the raw per-source row count in
    the index, ``realized`` is what was actually drawn per source (bounded by ``available`` --
    :func:`stratified_sql_ids` never allocates more than a source's real availability, so
    ``realized`` only ever comes up short of the allocation if the underlying table changed
    between the count and the draw).
    """
    con = sqlite3.connect(str(db_path))
    try:
        available = source_counts(con)
        allocation = stratified_sql_ids(available, total, floor)
        rows: list[dict[str, Any]] = []
        realized: dict[str, int] = {}
        for source in sorted(allocation):
            drawn = sample_source(con, source, allocation[source])
            rows.extend(drawn)
            realized[source] = len(drawn)
        return rows, available, realized
    finally:
        con.close()


def _log_stats(available: dict[str, int], realized: dict[str, int]) -> None:
    missing = sorted(set(available) - set(realized))
    if missing:
        print(
            "[sample_structured] sources with availability but 0 allocation "
            f"(budget/floor too tight): {', '.join(missing)}"
        )
    for source in sorted(realized):
        print(
            f"[sample_structured] {source}: available={available.get(source, 0)} "
            f"realized={realized[source]}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Sample structured (no-JD) rows from the prebuilt index, stratified per source."
    )
    parser.add_argument(
        "--out", required=True, help="Output JSONL path, e.g. bench/corpus_structured.jsonl."
    )
    parser.add_argument(
        "--total", type=int, required=True, help="Target row count for the written corpus."
    )
    parser.add_argument(
        "--floor",
        type=int,
        required=True,
        help="Minimum rows guaranteed per source (capped at that source's realized availability).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the index sqlite db. Defaults to the cached published index if present, "
        "else dist/index.sqlite.",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.exists():
        raise SystemExit(f"[sample_structured] no index db found at {db_path}")

    rows, available, realized = sample_structured(db_path, args.total, args.floor)
    _log_stats(available, realized)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_path, rows)
    sources = {row["source"] for row in rows}
    print(
        f"[sample_structured] wrote {len(rows)} rows spanning {len(sources)} sources -> {args.out}"
    )


if __name__ == "__main__":
    main()
