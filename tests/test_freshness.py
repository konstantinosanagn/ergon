"""Freshness filter: hide stale postings (max_age_days on max(posted_at, updated_at))."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ergon.index.build import build_index
from ergon.index.db import connect
from ergon.index.query import search_rows
from ergon.models import JobPosting, Location, RemoteType, SearchQuery

NOW = datetime.now(timezone.utc)


def _job(sid, title, posted=None, updated=None):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company="Co",
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
        posted_at=posted,
        updated_at=updated,
    )


def _titles(con, q):
    return {r["title"] for r in search_rows(con, q)}


def _db(tmp_path, jobs):
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    return connect(p, read_only=True)


def test_default_hides_stale_and_undated(tmp_path):
    con = _db(
        tmp_path,
        [
            _job("fresh", "Fresh Eng", posted=NOW - timedelta(days=10)),
            _job("stale", "Stale Eng", posted=NOW - timedelta(days=800)),  # >2yr
            _job("undated", "Undated Eng"),
        ],
    )
    assert _titles(con, SearchQuery(max_age_days=365)) == {"Fresh Eng"}
    assert _titles(con, SearchQuery(max_age_days=365, include_undated=True)) == {
        "Fresh Eng",
        "Undated Eng",
    }
    # opt out entirely
    assert _titles(con, SearchQuery(max_age_days=None)) == {"Fresh Eng", "Stale Eng", "Undated Eng"}
    # model default is None -> no filtering (existing callers unchanged)
    assert _titles(con, SearchQuery()) == {"Fresh Eng", "Stale Eng", "Undated Eng"}


def test_updated_at_rescues_reopened_posting(tmp_path):
    con = _db(
        tmp_path,
        [
            _job(
                "re",
                "Reopened Eng",
                posted=NOW - timedelta(days=800),
                updated=NOW - timedelta(days=5),
            ),
            _job(
                "dead",
                "Dead Eng",
                posted=NOW - timedelta(days=800),
                updated=NOW - timedelta(days=800),
            ),
        ],
    )
    assert _titles(con, SearchQuery(max_age_days=365)) == {"Reopened Eng"}  # fresh updated_at wins


def test_matches_freshness_in_memory():
    q = SearchQuery(max_age_days=365)
    assert q.matches(_job("a", "A", posted=NOW - timedelta(days=10)))
    assert not q.matches(_job("b", "B", posted=NOW - timedelta(days=800)))
    assert not q.matches(_job("c", "C"))  # undated dropped by default
    assert SearchQuery(max_age_days=365, include_undated=True).matches(_job("c", "C"))
    # updated_at rescue
    assert q.matches(
        _job("d", "D", posted=NOW - timedelta(days=800), updated=NOW - timedelta(days=5))
    )
    # opt out
    assert SearchQuery(max_age_days=None).matches(_job("e", "E", posted=NOW - timedelta(days=3000)))
