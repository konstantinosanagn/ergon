import sqlite3

from ergon.index.build import build_index
from ergon.index.db import connect
from ergon.index.query import _match_expr, search_rows
from ergon.models import JobLevel, JobPosting, Location, RemoteType, SearchQuery


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


def test_last_seen_staleness_guard(tmp_path):
    # A row whose board hasn't been re-confirmed within max_last_seen_age_days is hidden; a
    # recently-seen row survives. Both are status='active' (the ghost differs only by last_seen).
    import sqlite3
    from datetime import date, timedelta

    p = tmp_path / "i.sqlite"
    build_index([_job("1", "Fresh Engineer"), _job("2", "Stale Engineer")], p, build_id="b1")
    # Age job 2's last_seen to 40 days ago (an abandoned-board ghost carried forward).
    old = (date.today() - timedelta(days=40)).isoformat()
    w = sqlite3.connect(p)
    w.execute("UPDATE jobs SET last_seen = ? WHERE title = 'Stale Engineer'", (old,))
    w.commit()
    w.close()
    con = connect(p, read_only=True)

    # No guard -> both returned.
    assert len(search_rows(con, SearchQuery(keywords="engineer", limit=10))) == 2
    # Guard at 21 days -> the stale ghost is dropped, the fresh row survives.
    guarded = search_rows(
        con, SearchQuery(keywords="engineer", max_last_seen_age_days=21, limit=10)
    )
    assert [r["title"] for r in guarded] == ["Fresh Engineer"]
    # A generous window (60d) keeps both (never hides a slow-but-alive board).
    assert (
        len(search_rows(con, SearchQuery(keywords="engineer", max_last_seen_age_days=60, limit=10)))
        == 2
    )


def test_match_expr_one_and_two_tokens_unchanged():
    """1-2 terms keep the historical AND-of-quoted-terms SHAPE.

    Terms are no longer lowercased or stripped here: they go through raw inside quotes and FTS5
    tokenizes the contents with the table's own tokenizer. Asserting the literal expression string
    is what let the ASCII-only bug survive, so the behavioural test below is the real guard.
    """
    assert _match_expr("engineer") == '"engineer"'
    assert _match_expr("Software Engineer!") == '"Software" AND "Engineer!"'
    assert _match_expr("") == ""
    assert _match_expr('"""') == ""  # nothing searchable -> empty (caller returns no rows)


def test_match_expr_three_plus_tokens_phrase_or_near():
    # 3-4 tokens: exact phrase OR same-column NEAR group; every token individually quoted.
    assert _match_expr("Equity Research Associate") == (
        '("Equity Research Associate") OR (NEAR("Equity" "Research" "Associate", 10))'
    )
    # 4 tokens still stays phrase-OR-NEAR (the measured precision sweet spot) -- no any-token arm.
    assert _match_expr("senior backend distributed systems") == (
        '("senior backend distributed systems") OR '
        '(NEAR("senior" "backend" "distributed" "systems", 10))'
    )


def test_match_expr_five_plus_tokens_adds_any_token_or_arm():
    # Regression for "long query returns nothing": a 5+ token keyword BAG (a pasted sentence) can't
    # satisfy NEAR(all-N, 10), so phrase-OR-NEAR alone returned ZERO. An any-token OR arm is added so
    # FTS retrieves postings matching ANY term (BM25 re-rank then orders them); phrase + NEAR stay so
    # exact/proximity hits still rank on top.
    assert _match_expr("software engineer ai ml gpu systems") == (
        '("software engineer ai ml gpu systems") OR '
        '(NEAR("software" "engineer" "ai" "ml" "gpu" "systems", 10)) OR '
        '("software" OR "engineer" OR "ai" OR "ml" OR "gpu" OR "systems")'
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
    from ergon.models import Location

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


# --- behavioural guards: what the expression MATCHES, not what it looks like ------------------


def _fts(bodies: list[str]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute(
        'CREATE VIRTUAL TABLE t USING fts5(body, tokenize="porter unicode61 remove_diacritics 2")'
    )
    con.executemany("INSERT INTO t(body) VALUES (?)", [(b,) for b in bodies])
    return con


def _hits(con: sqlite3.Connection, keywords: str) -> int:
    expr = _match_expr(keywords)
    if not expr:
        return 0
    return con.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (expr,)).fetchone()[0]


def test_accented_query_matches_the_indexed_form() -> None:
    """The index stores `ingenieur` (remove_diacritics 2). Every casing/accenting must find it.

    Previously `[a-z0-9]+` shredded `Ingénieur` into ("ing", "nieur") and matched nothing —
    107,924 active rows carry non-ASCII titles.
    """
    con = _fts(["Ingénieur logiciel senior"])
    for q in ("Ingénieur", "ingénieur", "INGÉNIEUR", "ingenieur", "Ingenieur"):
        assert _hits(con, q) == 1, q


def test_non_latin_query_matches_rather_than_returning_everything() -> None:
    con = _fts(["エンジニア", "инженер", "软件工程师"])
    assert _hits(con, "エンジニア") == 1
    assert _hits(con, "инженер") == 1
    assert _hits(con, "软件工程师") == 1


def test_stemming_still_applies_through_the_quoted_term() -> None:
    """`Oberflächenbeschichter` stems to `oberflachenbeschicht`; passing the term raw gets that."""
    con = _fts(["Oberflächenbeschichter gesucht"])
    assert _hits(con, "Oberflächenbeschichter") == 1


def test_punctuation_in_a_term_is_tokenized_not_shredded() -> None:
    con = _fts(["Senior Software Engineer"])
    assert _hits(con, "Software Engineer!") == 1
    assert _hits(con, "software, engineer.") == 1


def test_fts_operators_in_user_input_stay_literal() -> None:
    """Injection safety: inside a quoted term the only special char is `"`, and it is doubled.

    The property is that an operator never ACTS as an operator — not that hostile input matches
    nothing. `engineer*` legitimately matches a doc containing "engineer", because FTS5 tokenizes
    the `*` away inside the quotes; that is the tokenizer working, not a prefix query.
    """
    con = _fts(["Senior Software Engineer", "totally unrelated pastry chef"])

    # 1. No hostile input may raise an FTS5 syntax error.
    for hostile in ('a" OR t MATCH "b', "engineer*", "NEAR(a b)", "^title", "engineer AND x", '"'):
        expr = _match_expr(hostile)
        if expr:
            con.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (expr,)).fetchone()

    # 2. A break-out must be a SINGLE term — terms are whitespace-split, so multi-word FTS syntax
    #    can never arrive as one term. `zzz"OR"engineer` is the real attack shape: without quote
    #    doubling it becomes `"zzz"OR"engineer"`, i.e. phrase-OR-phrase, and matches the engineer
    #    row. Escaped it is one literal string and matches nothing.
    probe = 'zzz"OR"engineer'
    escaped = _match_expr(probe)
    assert escaped == '"zzz""OR""engineer"'
    assert con.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (escaped,)).fetchone()[0] == 0
    # and the unescaped form really would have matched — so the assert above is load-bearing
    unescaped = '"zzz"OR"engineer"'
    assert con.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (unescaped,)).fetchone()[0] == 1
