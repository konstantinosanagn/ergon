from ergon_tracker.index.build import build_index
from ergon_tracker.index.db import connect
from ergon_tracker.index.query import _match_expr, search_rows
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


def test_match_expr_one_and_two_tokens_unchanged():
    # 1-2 token queries keep the historical AND-of-quoted-tokens semantics exactly.
    assert _match_expr("engineer") == '"engineer"'
    assert _match_expr("Software Engineer!") == '"software" AND "engineer"'
    assert _match_expr("") == ""
    assert _match_expr('"""') == ""  # no alphanumeric tokens -> empty (filter-only path)


def test_match_expr_three_plus_tokens_phrase_or_near():
    # 3+ tokens: exact phrase OR same-column NEAR group; every token individually quoted.
    assert _match_expr("Equity Research Associate") == (
        '("equity research associate") OR (NEAR("equity" "research" "associate", 10))'
    )


def test_multiword_query_excludes_cross_field_decoy(tmp_path):
    """Regression for the law-firm decoy: '2L Summer Associate' matched 'equity research
    associate' because plain AND matched each token in ANY FTS column (title had 'associate',
    snippet had 'research' and 'equity' in unrelated sentences). The phrase-OR-NEAR expression
    requires the tokens to co-occur in one column, so the decoy is excluded and the true
    title hit survives."""
    # Decoy: 'associate' only in the title; 'equity' and 'research' in the snippet, far apart
    # (> NEAR-10 window) and never adjacent — old AND semantics matched it, new must not.
    decoy_desc = (
        "Our private equity clients value advocacy above all else. "
        "Candidates should show strong analytical writing over many practice areas "
        "and enjoy independent legal research during the summer program."
    )
    con = _db(
        tmp_path,
        [
            _job("1", "Equity Research Associate", description_text="cover consumer stocks"),
            _job("2", "2L Summer Associate", company="Law LLP", description_text=decoy_desc),
        ],
    )
    # Guard against fixture rot: prove the decoy IS matched by the old AND expression.
    old_expr = '"equity" AND "research" AND "associate"'
    old_titles = {
        r[0]
        for r in con.execute(
            "SELECT j.title FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid "
            "WHERE jobs_fts MATCH ?",
            [old_expr],
        )
    }
    assert old_titles == {"Equity Research Associate", "2L Summer Associate"}

    rows = search_rows(con, SearchQuery(keywords="equity research associate", limit=10))
    assert [r["title"] for r in rows] == ["Equity Research Associate"]


def test_multiword_near_matches_out_of_order_title(tmp_path):
    # NEAR is order-insensitive: a reordered title ("Associate, Equity Research") still hits
    # even though the exact phrase does not; a same-column proximity match also hits.
    con = _db(
        tmp_path,
        [
            _job("1", "Associate, Equity Research", description_text="stocks"),
            _job(
                "2",
                "Investment Analyst",
                description_text="Join our equity research team as an associate covering banks.",
            ),
            _job("3", "Software Engineer", description_text="build services"),
        ],
    )
    rows = search_rows(con, SearchQuery(keywords="equity research associate", limit=10))
    titles = {r["title"] for r in rows}
    assert titles == {"Associate, Equity Research", "Investment Analyst"}


def test_multiword_injection_safety(tmp_path):
    """FTS5 operators/quotes/NEAR keywords as literal input must never reach the expression as
    syntax — only [a-z0-9]+ tokens are quoted into it — and must never raise."""
    con = _db(
        tmp_path,
        [
            _job("1", "Equity Research Associate", description_text="stocks"),
            _job("2", "Software Engineer", description_text="build services"),
        ],
    )
    for kw in (
        'equity research associate" OR 1=1 --',
        "NEAR(equity research, 5) associate",
        "equity) research (associate",
        'equity "research* associate^',
        "equity AND research OR associate NOT near",
        "'; DROP TABLE jobs; -- equity research associate",
    ):
        rows = search_rows(con, SearchQuery(keywords=kw, limit=10))
        assert isinstance(rows, list)  # no FTS5 syntax error, no SQL injection
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


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
