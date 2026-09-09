"""Build-time purge of the unambiguous-dead (>5yr) tail — physical drop, child tables + FTS consistent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ergon.index.build import build_index_streaming
from ergon.index.db import connect
from ergon.index.query import search_rows
from ergon.models import JobPosting, Location, RemoteType, SearchQuery

NOW = datetime.now(timezone.utc)


def _job(sid, posted=None, updated=None):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company="Co",
        title=sid,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
        posted_at=posted,
        updated_at=updated,
    )


def _yrs(n):
    return NOW - timedelta(days=365 * n)


def _build(tmp_path, jobs):
    p = tmp_path / "i.sqlite"
    build_index_streaming([jobs], p, build_id="b1")
    return connect(p, read_only=True)


def test_purges_dead_tail_keeps_the_rest(tmp_path):
    con = _build(
        tmp_path,
        [
            _job("fresh", posted=NOW - timedelta(days=10)),
            _job(
                "old3", posted=_yrs(3)
            ),  # stale but < 5yr -> kept (hidden by query filter, not deleted)
            _job("dead6", posted=_yrs(6)),  # > 5yr -> DROPPED
            _job(
                "reopened", posted=_yrs(7), updated=NOW - timedelta(days=5)
            ),  # old post, fresh update -> kept
            _job("undated"),  # no date -> kept (uncertain)
        ],
    )
    kept = {r[0] for r in con.execute("select title from jobs")}
    assert "dead6" not in kept
    assert {"fresh", "old3", "reopened", "undated"} <= kept


def test_no_orphans_and_fts_consistent(tmp_path):
    con = _build(
        tmp_path, [_job("dead", posted=_yrs(8)), _job("live", posted=NOW - timedelta(days=5))]
    )
    assert con.execute("select count(*) from jobs where title='dead'").fetchone()[0] == 0
    # job_sources for the purged job must be gone (no orphan rows)
    orphans = con.execute(
        "select count(*) from job_sources where job_id not in (select id from jobs)"
    ).fetchone()[0]
    assert orphans == 0
    # FTS rebuilt from survivors: search still works and the purged job is unfindable
    assert {r["title"] for r in search_rows(con, SearchQuery(max_age_days=None))} == {"live"}


def test_purge_returns_count(tmp_path):
    # _purge_ancient returns how many it dropped (used for build logging). Set up via the non-purging
    # build_index, then call _purge_ancient directly.
    from ergon.index.build import _purge_ancient, build_index

    p = tmp_path / "i.sqlite"
    build_index(
        [_job("live", posted=NOW - timedelta(days=5)), _job("zombie", posted=_yrs(10))],
        p,
        build_id="b1",
    )
    con = connect(p)  # writable
    assert con.execute("select count(*) from jobs where title='zombie'").fetchone()[0] == 1
    assert _purge_ancient(con) == 1
    assert con.execute("select count(*) from jobs where title='zombie'").fetchone()[0] == 0
