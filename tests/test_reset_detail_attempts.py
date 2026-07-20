"""Tests for scripts/reset_detail_attempts.py -- the RETRY_CAP rescue for stuck Tier-3 rows.

Offline, stdlib sqlite only. Builds a synthetic detail sidecar (real schema via ``open_detail``)
with mixed rows and asserts ONLY the empty-snippet capped rows get their ``attempts`` reset, plus
idempotency (a second run is a no-op)."""

from __future__ import annotations

import sqlite3

from scripts.reset_detail_attempts import count_stuck, reset_stuck_attempts

from ergon_tracker.index.detail import RETRY_CAP, open_detail


def _seed(path: str) -> None:
    con = open_detail(path)
    # (id, attempts, snippet) -- the three columns this rescue cares about.
    rows = [
        ("capped-empty-null", RETRY_CAP, None),  # RESCUE: capped + NULL snippet
        ("capped-empty-blank", RETRY_CAP, "   "),  # RESCUE: capped + whitespace-only snippet
        ("capped-recovered", RETRY_CAP, "Real JD snippet here."),  # KEEP: has a snippet (a success)
        ("fresh-empty", 0, None),  # already 0 + empty -> matched, but a no-op reset
        ("midway-empty", 1, ""),  # partial budget + empty -> rescued back to 0
        ("recovered-zero", 0, "Another snippet."),  # KEEP: success, untouched
    ]
    con.executemany(
        "INSERT INTO job_detail (id, sig, attempts, snippet) VALUES (?, 's', ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _attempts(path: str) -> dict[str, int]:
    con = sqlite3.connect(path)
    out = {r[0]: r[1] for r in con.execute("SELECT id, attempts FROM job_detail")}
    con.close()
    return out


def test_resets_only_empty_snippet_rows(tmp_path):
    db = str(tmp_path / "index-detail.sqlite")
    _seed(db)

    n = reset_stuck_attempts(db)
    # Matched = every empty-snippet row (3 non-zero-attempt + 1 already-zero): null/blank/empty.
    assert n == 4

    after = _attempts(db)
    # Every empty-snippet row is now 0 ...
    assert after["capped-empty-null"] == 0
    assert after["capped-empty-blank"] == 0
    assert after["fresh-empty"] == 0
    assert after["midway-empty"] == 0
    # ... and NO snippet-bearing (recovered) row was touched -- its retry budget is preserved.
    assert after["capped-recovered"] == RETRY_CAP
    assert after["recovered-zero"] == 0  # was already 0, still 0 (never had a reason to change)


def test_idempotent_second_run_changes_nothing(tmp_path):
    db = str(tmp_path / "index-detail.sqlite")
    _seed(db)

    reset_stuck_attempts(db)
    first = _attempts(db)

    # A second run matches the same converged empty-snippet set but changes no values.
    n2 = reset_stuck_attempts(db)
    second = _attempts(db)
    assert n2 == 4  # same rows still match (they stay empty-snippet)
    assert first == second  # but nothing actually moved


def test_dry_run_writes_nothing(tmp_path):
    db = str(tmp_path / "index-detail.sqlite")
    _seed(db)

    before = _attempts(db)
    n = reset_stuck_attempts(db, dry_run=True)
    after = _attempts(db)

    assert n == 4  # reports the count that WOULD be reset
    assert before == after  # ... but the capped rows still hold their attempts
    assert after["capped-empty-null"] == RETRY_CAP


def test_count_stuck_matches_empty_snippet_set(tmp_path):
    db = str(tmp_path / "index-detail.sqlite")
    _seed(db)
    con = open_detail(db)
    try:
        assert count_stuck(con) == 4
    finally:
        con.close()


def test_missing_sidecar_creates_empty_and_rescues_zero(tmp_path):
    # open_detail is idempotent -- pointing at a not-yet-existing db creates an empty one (0 rows).
    db = str(tmp_path / "does-not-exist-yet.sqlite")
    assert reset_stuck_attempts(db) == 0
