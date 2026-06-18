from ergon_tracker.index.build import build_index
from ergon_tracker.index.db import connect
from ergon_tracker.index.query import search_rows
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


def _db(tmp_path, jobs):
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    return connect(p, read_only=True)


def test_keyword_ranks_title_match_first(tmp_path):
    con = _db(
        tmp_path,
        [
            _job("1", "Account Executive",
                 description_text="work with engineering and engineer teams"),
            _job("2", "Software Engineer", description_text="build services"),
        ],
    )
    rows = search_rows(con, SearchQuery(keywords="engineer", limit=5))
    assert rows[0]["title"] == "Software Engineer"


def test_filter_only_path_and_level_filter(tmp_path):
    # distinct titles so the builder's dedup keeps both rows
    con = _db(
        tmp_path,
        [_job("1", "Backend Engineer", level=JobLevel.SENIOR),
         _job("2", "Frontend Engineer", level=JobLevel.MID)],
    )
    rows = search_rows(con, SearchQuery(level=JobLevel.SENIOR, limit=10))
    assert len(rows) == 1 and rows[0]["level"] == "senior"


def test_matches_parity_on_filters(tmp_path):
    # distinct titles -> no dedup -> index holds all three (parity vs matches() is meaningful)
    jobs = [
        _job("1", "Backend Engineer", level=JobLevel.SENIOR, sector="Fintech"),
        _job("2", "Frontend Engineer", level=JobLevel.MID, sector="Fintech"),
        _job("3", "Data Engineer", level=JobLevel.SENIOR, sector=None),
    ]
    con = _db(tmp_path, jobs)
    for q in [
        SearchQuery(level=JobLevel.SENIOR),
        SearchQuery(sector="Fintech"),
        SearchQuery(sector="Fintech", include_unknown_sector=True),
        SearchQuery(level=JobLevel.SENIOR, include_unknown_level=True),
    ]:
        sql_ids = {r["id"] for r in search_rows(con, q)}
        match_ids = {j.id for j in jobs if q.matches(j)}
        assert sql_ids == match_ids, f"parity broke for {q}"
