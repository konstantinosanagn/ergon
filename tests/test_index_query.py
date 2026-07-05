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
            _job(
                "1",
                "Account Executive",
                description_text="work with engineering and engineer teams",
            ),
            _job("2", "Software Engineer", description_text="build services"),
        ],
    )
    rows = search_rows(con, SearchQuery(keywords="engineer", limit=5))
    assert rows[0]["title"] == "Software Engineer"


def test_filter_only_path_and_level_filter(tmp_path):
    # distinct titles so the builder's dedup keeps both rows
    con = _db(
        tmp_path,
        [
            _job("1", "Backend Engineer", level=JobLevel.SENIOR),
            _job("2", "Frontend Engineer", level=JobLevel.MID),
        ],
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


def test_query_robust_against_adversarial_and_edge_input(tmp_path):
    """FTS keyword path must never break or SQL-inject; edge inputs return sane results.

    Locks in the live stress-test result: tokens are quoted before reaching FTS5, so
    operators / quotes / specials are treated as literals, never as query syntax or SQL.
    """
    con = _db(
        tmp_path,
        [
            _job("1", "Senior Software Engineer", description_text="c++ and python"),
            _job("2", "Data Scientist", description_text="ml research"),
            _job("3", "Account Executive", description_text="sales"),
        ],
    )
    # adversarial keyword strings must not raise and must not return the whole table via injection
    for kw in (
        'engineer" OR 1=1 --',
        "c++ (senior) AND/OR *",
        "AND OR NOT NEAR",
        "'; DROP TABLE jobs; --",
        '"""',
        "ingénieur café",
        "engineer " * 200,
    ):
        rows = search_rows(con, SearchQuery(keywords=kw, limit=50))
        assert isinstance(rows, list)
        assert all(r["title"] and r["company"] for r in rows)

    # the injection table is intact (DROP did not execute)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3

    # edge cases
    assert search_rows(con, SearchQuery(keywords="zzxqkjwffbbq", limit=5)) == []  # no match
    assert len(search_rows(con, SearchQuery(keywords="engineer", limit=1))) == 1  # limit honored
    assert len(search_rows(con, SearchQuery(keywords="engineer", limit=100000))) <= 3  # huge limit


def test_degree_filter_round_trip(tmp_path):
    # Build a tiny index and filter with max_degree=bachelor: a William Blair-style posting
    # (phd_md, preferred-only) must be EXCLUDED, a bachelor posting included, and an
    # unspecified-degree posting included by default (include_unknown_degree=True).
    jobs = [
        _job("1", "Equity Research Associate", degree_min="phd_md", degree_required=False),
        _job("2", "Software Engineer", degree_min="bachelor", degree_required=True),
        _job("3", "Account Executive"),  # no stated degree
    ]
    con = _db(tmp_path, jobs)

    rows = search_rows(con, SearchQuery(max_degree="bachelor", limit=10))
    titles = {r["title"] for r in rows}
    assert titles == {"Software Engineer", "Account Executive"}

    strict = search_rows(
        con, SearchQuery(max_degree="bachelor", include_unknown_degree=False, limit=10)
    )
    assert {r["title"] for r in strict} == {"Software Engineer"}

    # ceiling high enough -> everything (incl. the phd_md posting) matches
    assert len(search_rows(con, SearchQuery(max_degree="phd_md", limit=10))) == 3

    # parity with SearchQuery.matches() on the same set
    for q in [
        SearchQuery(max_degree="bachelor"),
        SearchQuery(max_degree="bachelor", include_unknown_degree=False),
        SearchQuery(max_degree="master"),
        SearchQuery(max_degree="highschool", include_unknown_degree=False),
    ]:
        sql_ids = {r["id"] for r in search_rows(con, q)}
        match_ids = {j.id for j in jobs if q.matches(j)}
        assert sql_ids == match_ids, f"degree parity broke for {q}"


def test_matches_parity_on_location(tmp_path):
    # The index must filter on the free-text `location` exactly like SearchQuery.matches() —
    # regression for the index silently ignoring `location` (returned non-matching jobs).
    from ergon_tracker.models import Location

    jobs = [
        JobPosting.create(
            source="greenhouse",
            source_job_id="1",
            company="A",
            title="Eng Berlin",
            locations=[Location(raw="Berlin, Germany", city="Berlin", country="Germany")],
        ),
        JobPosting.create(
            source="greenhouse",
            source_job_id="2",
            company="B",
            title="Eng London",
            locations=[Location(raw="London, UK", city="London", country="United Kingdom")],
        ),
        JobPosting.create(
            source="greenhouse",
            source_job_id="3",
            company="C",
            title="Eng NYC",
            locations=[Location(raw="New York, US", city="New York", country="United States")],
        ),
    ]
    con = _db(tmp_path, jobs)
    for q in [
        SearchQuery(location="Germany"),
        SearchQuery(location="London"),
        SearchQuery(location="New York"),
        SearchQuery(location="zzz-nowhere"),
    ]:
        sql_ids = {r["id"] for r in search_rows(con, q)}
        match_ids = {j.id for j in jobs if q.matches(j)}
        assert sql_ids == match_ids, f"location parity broke for {q.location!r}"
