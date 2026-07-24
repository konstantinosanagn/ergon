"""Full-JD sidecar: ``id -> gzip(full JD text)`` — the replay source for re-extraction.

The core index DISCARDS the full JD after extraction: ``mapping.to_row`` stores only a 300-char
``snippet`` (``_SNIPPET``), so a future/improved extractor can never re-read more than 300 chars and
retroactive re-enrichment is impossible without a full re-crawl. This sidecar persists the full JD
text so extraction becomes **replayable WITHOUT re-crawl** — the storage foundation the re-enrich
pass (pipeline-restructuring Item 3) consumes via :func:`get`.

**Design (mirrors the detail/rich sidecars):** a SEPARATE ``index-jd.sqlite`` keyed by posting id,
NOT a core-index column — the ~hundreds-of-MB core index is untouched (zero core-schema churn, so
the `jobs` parity gate holds trivially), and it follows the same lifecycle as ``index-detail.sqlite``
/ ``index-vectors.sqlite``: carried forward build-to-build (seeded from the prior published gz),
pruned to the live index ids, and published as ``index-jd.sqlite.gz`` with a manifest.

Each JD is gzip-compressed **per row** (not one stream over the whole table) so :func:`get` can
decompress a SINGLE posting's JD by id without reading the corpus — random-access replay. Measured
on the real ~1M-JD corpus this is ~0.7-1.0 GB compressed (English JD text gzips ~3.7-4.9x); large
but opt-in, exactly like the detail/vectors sidecars.
"""

from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Iterable, Sequence

JD_SCHEMA_VERSION = 1
JD_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_jd (id TEXT PRIMARY KEY, jd BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Level 6 matches the sidecar/artifact gzip default used elsewhere (build_index._GZIP_LEVEL): a good
# ratio without the CPU cost of 9, and the per-row compress runs under the crawl's write_lock so it
# must stay cheap. ``mtime=0`` makes a given JD's compressed bytes DETERMINISTIC (no embedded
# timestamp), so an unchanged JD upserts identical bytes build-to-build.
_GZIP_LEVEL = 6


def _compress(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=_GZIP_LEVEL, mtime=0)


def _decompress(blob: bytes) -> str:
    return gzip.decompress(blob).decode("utf-8")


def ensure_jd_schema(con: sqlite3.Connection) -> None:
    con.executescript(JD_SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(JD_SCHEMA_VERSION),),
    )
    con.commit()


def open_jd_store(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    ensure_jd_schema(con)
    return con


def put(con: sqlite3.Connection, id: str, jd_text: str | None) -> None:
    """Store (gzip-compress) the full JD text for ``id``. Empty/None text is a no-op — the sidecar
    only ever holds a real JD, so ``get`` returning ``None`` means "no JD captured", never "empty".
    Upserts: a re-crawled posting whose JD changed overwrites the stored blob (fresher wins)."""
    if not jd_text:
        return
    con.execute(
        "INSERT INTO job_jd(id, jd) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET jd = excluded.jd",
        (id, _compress(jd_text)),
    )


def put_many(con: sqlite3.Connection, items: Iterable[tuple[str, str | None]]) -> int:
    """Batch upsert ``(id, jd_text)`` pairs — the crawl path, called once per board under the shared
    ``write_lock`` so the per-row gzip never becomes a per-posting bottleneck (one executemany, one
    lock acquisition per board, not per JD). Skips empty JDs. Returns the number stored."""
    rows = [(i, _compress(t)) for i, t in items if t]
    if not rows:
        return 0
    con.executemany(
        "INSERT INTO job_jd(id, jd) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET jd = excluded.jd",
        rows,
    )
    return len(rows)


def get(con: sqlite3.Connection, id: str) -> str | None:
    """Decompress and return the full JD text for ``id`` (the re-enrich replay source, Item 3), or
    ``None`` when no JD is stored. Round-trips exactly: ``get`` returns the identical text ``put`` was
    given (gzip is lossless; the stored bytes are the only transform)."""
    row = con.execute("SELECT jd FROM job_jd WHERE id = ?", (id,)).fetchone()
    return _decompress(row[0]) if row is not None else None


def count(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM job_jd").fetchone()[0])


def carry_forward(dst_con: sqlite3.Connection, prior_path: str) -> int:
    """Union a PRIOR jd-store (``prior_path``) into ``dst_con`` WITHOUT clobbering an id already
    present — the freshly-crawled JD in ``dst_con`` always wins (INSERT OR IGNORE), a carried-forward
    JD only fills an id this build did not re-crawl. Returns the number of ids carried forward.

    NOTE: the production build carries forward by *seeding* ``index-jd.sqlite`` from the prior
    published gz (the workflow gunzips it onto the same path the crawl then upserts onto), so this
    explicit merge is the unit-testable primitive for that lifecycle and a fallback for callers that
    keep the prior sidecar as a separate file. A missing/absent prior file is non-fatal (returns 0)."""
    import os

    if not prior_path or not os.path.exists(prior_path):
        return 0
    dst_con.execute("ATTACH DATABASE ? AS prior", (prior_path,))
    try:
        cur = dst_con.execute(
            "INSERT OR IGNORE INTO job_jd(id, jd) SELECT id, jd FROM prior.job_jd"
        )
        moved = cur.rowcount
        dst_con.commit()
    finally:
        dst_con.execute("DETACH DATABASE prior")
    return moved


def prune_to_live_ids(con: sqlite3.Connection, live_ids: Sequence[str] | set[str]) -> int:
    """Drop every stored JD whose id is NOT in ``live_ids`` (the promoted index's ``jobs`` ids), so
    the sidecar stays bounded to postings the index still serves — mirrors the rich reconcile's
    orphan cascade. Uses a temp table (not a giant ``IN (...)``) to scale past SQLite's bound-variable
    ceiling. Returns the number of orphan JDs removed."""
    con.execute("CREATE TEMP TABLE _live_ids (id TEXT PRIMARY KEY)")
    try:
        con.executemany("INSERT OR IGNORE INTO _live_ids(id) VALUES (?)", ((i,) for i in live_ids))
        cur = con.execute("DELETE FROM job_jd WHERE id NOT IN (SELECT id FROM _live_ids)")
        removed = cur.rowcount
    finally:
        con.execute("DROP TABLE _live_ids")
    con.commit()
    return removed
