"""Proves the Task-5-review fix: `jobs_fts` must be rebuilt after the Tier-3 detail merge, or a
recovered snippet lands in `jobs.snippet` but stays invisible to `jobs_fts MATCH` keyword queries.

`jobs_fts` is an external-content FTS5 table (schema.sql) with NO sync triggers on `jobs` -- a
plain `UPDATE jobs SET snippet = ...` (exactly what `merge_detail_into_index` does) never touches
the FTS index. Mirrors the real build ordering: the core build rebuilds `jobs_fts` once (BEFORE
the Tier-3 pass ever runs), then `reconcile_detail_tier` + `merge_detail_into_index` write the
recovered snippet straight into `jobs`, and only `_rebuild_jobs_fts` (scripts/build_index.py)
makes it searchable again.

Offline/deterministic: fake `fetch_detail`, injected `now`, real schema via `db.fresh_db`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_index import _rebuild_jobs_fts  # noqa: E402

from ergon_tracker.index.db import fresh_db  # noqa: E402
from ergon_tracker.index.detail import merge_detail_into_index, reconcile_detail_tier  # noqa: E402

_NOW = "2026-07-12T00:00:00Z"
_RARE_WORD = "quargleflux"  # appears only in the recovered JD's snippet, nowhere else in the row


def _build_index(tmp_path: Path) -> str:
    """A real-schema index (via `fresh_db`) with one smartrecruiters row, empty `snippet` (the
    Tier-3 candidate signal), then an initial `jobs_fts` rebuild -- mirroring the core build's
    rebuild that runs BEFORE the detail tier ever sees the db (see build.py's build_index)."""
    p = tmp_path / "index.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    con.execute(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token, "
        "apply_url, listing_url, snippet) "
        "VALUES ('sr-1', 'ch-1', 'smartrecruiters', 'Acme', 'Platform Engineer', 'unknown', "
        "'mid', 'full_time', 'active', ?, ?, ?, 'b1', 'acme', "
        "'https://jobs.smartrecruiters.com/acme/12345', NULL, NULL)",
        (_NOW, _NOW, _NOW),
    )
    con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")  # baseline core-build rebuild
    con.commit()
    con.close()
    return str(p)


def _match_ids(db_path: str, word: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT j.id FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid WHERE jobs_fts MATCH ?",
            (word,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def test_fts_rebuild_makes_recovered_snippet_searchable(tmp_path):
    idx = _build_index(tmp_path)
    det = str(tmp_path / "detail.sqlite")

    async def fake(ref):
        return f"<p>Great role, req {ref.id}. Uses a {_RARE_WORD} pipeline daily.</p>"

    # Baseline: the rare word appears nowhere yet -- not in the row, not in the (empty) FTS.
    assert _match_ids(idx, _RARE_WORD) == []

    stats = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: _NOW))
    assert stats["fetched"] == 1

    index_con = sqlite3.connect(idx)
    try:
        merged = merge_detail_into_index(index_con, det)
    finally:
        index_con.close()
    assert merged == 1  # the merge wrote jobs.snippet

    # Sanity: the merge really did land the recovered snippet in `jobs.snippet`.
    snippet = sqlite3.connect(idx).execute("SELECT snippet FROM jobs WHERE id='sr-1'").fetchone()[0]
    assert snippet and _RARE_WORD in snippet.lower()

    # BEFORE the fix's rebuild: `jobs_fts` is external-content and has no sync triggers, so the
    # plain UPDATE `merge_detail_into_index` issued is invisible to it -- MATCH still finds nothing.
    assert _match_ids(idx, _RARE_WORD) == [], (
        "jobs_fts matched the recovered word BEFORE any rebuild -- "
        "test assumption about external-content FTS5 sync no longer holds"
    )

    # THE FIX: rebuild jobs_fts over the merged db (same call build_and_publish_detail now makes).
    _rebuild_jobs_fts(Path(idx))

    # AFTER: the recovered snippet is now searchable via the real keyword-query path (jobs_fts MATCH).
    assert _match_ids(idx, _RARE_WORD) == ["sr-1"]
