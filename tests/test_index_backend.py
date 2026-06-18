from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType, SearchQuery


def _job(sid, title, **kw):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=kw.pop("company", "Co"),
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def test_backend_search_returns_jobpostings_with_provenance(tmp_path):
    p = tmp_path / "i.sqlite"
    build_index([_job("1", "Senior Backend Engineer", level=JobLevel.SENIOR)], p, build_id="b1")
    be = SqliteIndexBackend(p)
    assert be.available() is True
    assert be.metadata()["row_count"] == 1
    jobs = be.search(SearchQuery(keywords="backend", limit=5))
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].provenance and jobs[0].provenance[0].source == "greenhouse"


def test_backend_unavailable_when_missing(tmp_path):
    assert SqliteIndexBackend(tmp_path / "nope.sqlite").available() is False
