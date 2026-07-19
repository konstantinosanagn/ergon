"""Phase 2 of the daily freshness sweep (docs/superpowers/specs/2026-07-18-daily-freshness-sweep-
design.md): the daily build_index must carry forward expiries a prior freshness-sweep run wrote to
a published ``index-freshness.sqlite`` sidecar, so a full rebuild never resurrects a posting the
sweep already confirmed departed its board.

Pinned sidecar contract (the sweep's future writer must match this exactly):
    a SQLite DB with one table ``expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL,
    reason TEXT)``.

``apply_freshness_expiries`` must never hard-delete (COUNT(*) stays unchanged -> the row_floor
publish gate is safe) and must never raise (an absent/malformed sidecar degrades to a no-op),
mirroring ``carry_forward``'s ATTACH-failure contract.
"""

from __future__ import annotations

import sqlite3

from ergon_tracker.index.build import apply_freshness_expiries, build_index_from_fresh_db
from ergon_tracker.index.db import connect
from ergon_tracker.index.query import search_rows
from ergon_tracker.models import Location, RemoteType, SearchQuery

_JOBS_TABLE_SQL = "CREATE TABLE jobs(id TEXT PRIMARY KEY, status TEXT)"


def _make_job(source_job_id: str, *, title: str = "Backend Engineer"):
    from ergon_tracker.models import JobPosting

    return JobPosting.create(
        source="greenhouse",
        source_job_id=source_job_id,
        company="Acme Inc",
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
    )


def _fresh_db_with_jobs(path, jobs, *, build_id) -> None:
    from ergon_tracker.index.build import append_jobs
    from ergon_tracker.index.db import fresh_db

    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        append_jobs(con, jobs, build_id=build_id)
        con.commit()
    finally:
        con.close()


def _write_freshness_sidecar(path, rows: list[tuple[str, str, str | None]]) -> None:
    """rows: list of (id, expired_at, reason)."""
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)"
        )
        con.executemany("INSERT INTO expired_ids(id, expired_at, reason) VALUES (?,?,?)", rows)
        con.commit()
    finally:
        con.close()


def test_two_build_sequence_carries_expiry_forward(tmp_path):
    x = _make_job("job-x", title="Backend Engineer")
    y = _make_job("job-y", title="Frontend Engineer")

    # Day 1: build an index with X and Y both active.
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x, y], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    # A synthetic prior freshness-sweep sidecar marking X (only) expired.
    freshness_db = tmp_path / "index-freshness.sqlite"
    _write_freshness_sidecar(freshness_db, [(x.id, "2026-07-18T00:00:00Z", "departed_board")])

    # Day 2: rebuild (nothing re-crawled -> everything carries forward from day1).
    fresh2 = tmp_path / "fresh2.sqlite"
    _fresh_db_with_jobs(fresh2, [], build_id="b1")
    day2 = tmp_path / "day2.sqlite"
    build_index_from_fresh_db(fresh2, day2, build_id="b1", prev_db=day1, crawled_keys=set())

    before_count = connect(day2, read_only=True).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert before_count == 2

    con = connect(day2)
    try:
        updated = apply_freshness_expiries(con, freshness_db)
        con.commit()
        assert updated == 1

        after_count = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert after_count == before_count  # never hard-deleted

        xrow = con.execute(
            "SELECT status, expired_at, expiry_reason FROM jobs WHERE id=?", (x.id,)
        ).fetchone()
        assert xrow["status"] == "expired"
        assert xrow["expired_at"] == "2026-07-18T00:00:00Z"
        assert xrow["expiry_reason"] == "departed_board"

        yrow = con.execute("SELECT status FROM jobs WHERE id=?", (y.id,)).fetchone()
        assert yrow["status"] == "active"

        # search_rows (status filter) must exclude the expired posting.
        results = search_rows(con, SearchQuery())
        result_ids = {r["id"] for r in results}
        assert x.id not in result_ids
        assert y.id in result_ids
    finally:
        con.close()


def test_absent_sidecar_returns_zero(tmp_path):
    x = _make_job("job-x")
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    con = connect(day1)
    try:
        missing_path = tmp_path / "does-not-exist.sqlite"
        updated = apply_freshness_expiries(con, missing_path)
        assert updated == 0
        row = con.execute("SELECT status FROM jobs WHERE id=?", (x.id,)).fetchone()
        assert row["status"] == "active"
    finally:
        con.close()


def test_malformed_sidecar_missing_table_returns_zero(tmp_path):
    x = _make_job("job-x")
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    # A valid sqlite DB, but WITHOUT the expired_ids table.
    bad_db = tmp_path / "bad.sqlite"
    bcon = sqlite3.connect(str(bad_db))
    bcon.execute("CREATE TABLE something_else(id TEXT)")
    bcon.commit()
    bcon.close()

    con = connect(day1)
    try:
        updated = apply_freshness_expiries(con, bad_db)
        assert updated == 0
        row = con.execute("SELECT status FROM jobs WHERE id=?", (x.id,)).fetchone()
        assert row["status"] == "active"
    finally:
        con.close()


def test_malformed_sidecar_bad_file_returns_zero(tmp_path):
    x = _make_job("job-x")
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    # Not a sqlite file at all (e.g. a truncated/corrupt gunzip, or a leftover .gz never unzipped).
    bad_db = tmp_path / "garbage.sqlite"
    bad_db.write_bytes(b"not-a-sqlite-database-at-all" * 10)

    con = connect(day1)
    try:
        updated = apply_freshness_expiries(con, bad_db)
        assert updated == 0
        row = con.execute("SELECT status FROM jobs WHERE id=?", (x.id,)).fetchone()
        assert row["status"] == "active"
    finally:
        con.close()


def test_sidecar_id_not_in_index_is_noop(tmp_path):
    x = _make_job("job-x")
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    freshness_db = tmp_path / "index-freshness.sqlite"
    _write_freshness_sidecar(
        freshness_db, [("some-id-not-in-the-index", "2026-07-18T00:00:00Z", "departed_board")]
    )

    con = connect(day1)
    try:
        updated = apply_freshness_expiries(con, freshness_db)
        assert updated == 0
        row = con.execute("SELECT status FROM jobs WHERE id=?", (x.id,)).fetchone()
        assert row["status"] == "active"
    finally:
        con.close()


def test_already_expired_row_is_idempotent(tmp_path):
    x = _make_job("job-x")
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    freshness_db = tmp_path / "index-freshness.sqlite"
    _write_freshness_sidecar(freshness_db, [(x.id, "2026-07-18T00:00:00Z", "departed_board")])

    con = connect(day1)
    try:
        first = apply_freshness_expiries(con, freshness_db)
        con.commit()
        assert first == 1

        # calling again against the same (still-active-elsewhere) sidecar must be a safe no-op
        second = apply_freshness_expiries(con, freshness_db)
        con.commit()
        assert second == 0

        row = con.execute(
            "SELECT status, expired_at, expiry_reason FROM jobs WHERE id=?", (x.id,)
        ).fetchone()
        assert row["status"] == "expired"
        assert row["expired_at"] == "2026-07-18T00:00:00Z"
        assert row["expiry_reason"] == "departed_board"
    finally:
        con.close()


def test_reason_defaults_to_departed_board_when_null(tmp_path):
    x = _make_job("job-x")
    fresh1 = tmp_path / "fresh1.sqlite"
    _fresh_db_with_jobs(fresh1, [x], build_id="b0")
    day1 = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1, build_id="b0")

    freshness_db = tmp_path / "index-freshness.sqlite"
    _write_freshness_sidecar(freshness_db, [(x.id, "2026-07-18T00:00:00Z", None)])

    con = connect(day1)
    try:
        updated = apply_freshness_expiries(con, freshness_db)
        assert updated == 1
        row = con.execute("SELECT expiry_reason FROM jobs WHERE id=?", (x.id,)).fetchone()
        assert row["expiry_reason"] == "departed_board"
    finally:
        con.close()
