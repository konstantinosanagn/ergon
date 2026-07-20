"""``backfill_board_tokens`` fills jobs.board_token for carried-forward active rows (NULL until the
crawl re-visits the board) from the seed registry, so the freshness sweep covers ~all boards
immediately instead of ramping over the ~5-day crawl cycle. Registry token == the crawl's fetch
token (exact by construction); the backfill never overwrites an existing token and only touches
active rows. Registry maps are injected here for a deterministic offline test."""

from __future__ import annotations

from ergon_tracker.index.build import (
    _default_url_parsers,
    _derive_token_from_url,
    backfill_board_tokens,
)
from ergon_tracker.index.db import connect, fresh_db
from ergon_tracker.index.freshness import DETERMINISTIC_SOURCES, SEARCH_INDEX_SOURCES


def _insert(
    con,
    jid,
    *,
    source,
    company_key,
    domain=None,
    board_token=None,
    status="active",
    apply_url=None,
    listing_url=None,
):
    con.execute(
        "INSERT INTO jobs(id, content_hash, company_key, source, company, company_domain, title, "
        "remote, level, employment_type, board_token, status, first_seen, last_seen, fetched_at, "
        "build_id, apply_url, listing_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            jid, f"h-{jid}", company_key, source, "Co", domain, "Engineer",
            "unknown", "mid", "fulltime", board_token, status, "t", "t", "t", "b",
            apply_url, listing_url,
        ),
    )


def _token(con, jid):
    return con.execute("SELECT board_token FROM jobs WHERE id=?", (jid,)).fetchone()[0]


def test_backfill_fills_from_registry_key_and_domain_only(tmp_path):
    path = tmp_path / "idx.sqlite"
    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        # by company_key match
        _insert(con, "a", source="greenhouse", company_key="acme")
        # by domain match (no key match)
        _insert(con, "b", source="lever", company_key="nokey", domain="beta.com")
        # already has a token -> must NOT be overwritten
        _insert(con, "c", source="greenhouse", company_key="acme", board_token="preset")
        # no registry match -> stays NULL
        _insert(con, "d", source="ashby", company_key="unknown", domain="none.com")
        # expired row with a key match -> backfill only touches active rows
        _insert(con, "e", source="greenhouse", company_key="acme", status="expired")
        con.commit()

        by_key = {("acme", "greenhouse"): "acme-board"}
        by_dom = {("beta.com", "lever"): "beta-board"}
        n = backfill_board_tokens(con, by_key=by_key, by_dom=by_dom)
        con.commit()

        assert n == 2  # only a and b
        assert _token(con, "a") == "acme-board"  # registry key
        assert _token(con, "b") == "beta-board"  # registry domain fallback
        assert _token(con, "c") == "preset"  # existing token preserved
        assert _token(con, "d") is None  # no match -> untouched
        assert _token(con, "e") is None  # expired row untouched

        # idempotent: a second run has nothing left to fill
        assert backfill_board_tokens(con, by_key=by_key, by_dom=by_dom) == 0
    finally:
        con.close()


def test_backfill_key_beats_domain(tmp_path):
    path = tmp_path / "idx.sqlite"
    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        _insert(con, "a", source="greenhouse", company_key="acme", domain="acme.com")
        con.commit()
        by_key = {("acme", "greenhouse"): "from-key"}
        by_dom = {("acme.com", "greenhouse"): "from-domain"}
        backfill_board_tokens(con, by_key=by_key, by_dom=by_dom)
        con.commit()
        assert _token(con, "a") == "from-key"  # company_key takes precedence over domain
    finally:
        con.close()


# --- apply-URL derivation fallback (search-index sources only) --------------------------------


def test_url_derivation_fires_only_for_search_index_sources(tmp_path):
    """The gate: URL derivation is applied only to a source in BOTH the parser map AND the
    freshness search-index set. A deterministic source with a perfectly URL-derivable apply_url
    (its own parser present) must NOT get a URL-derived token -- otherwise a wrong token could
    false-expire a live row via the deterministic single-miss path. Uses a fake always-matching
    parser so the test isolates the gate, not any provider's URL format."""
    path = tmp_path / "idx.sqlite"
    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        # a real search-index source (workday) -> should URL-derive
        _insert(con, "si", source="workday", company_key="nokey", apply_url="x")
        # a real deterministic source (greenhouse) with a parser present -> must stay NULL
        _insert(con, "det", source="greenhouse", company_key="nokey", apply_url="x")
        con.commit()

        fake = {"workday": lambda _u: "WD-TOK", "greenhouse": lambda _u: "GH-TOK"}
        n = backfill_board_tokens(
            con,
            by_key={},
            by_dom={},
            url_parsers=fake,
            search_index_sources=SEARCH_INDEX_SOURCES,  # greenhouse NOT in here
        )
        con.commit()

        assert n == 1
        assert _token(con, "si") == "WD-TOK"  # search-index -> derived
        assert _token(con, "det") is None  # deterministic -> gate blocks derivation

        # Prove the gate is what blocks: allow greenhouse into the search-index set and it now fills.
        backfill_board_tokens(
            con,
            by_key={},
            by_dom={},
            url_parsers=fake,
            search_index_sources={"workday", "greenhouse"},
        )
        con.commit()
        assert _token(con, "det") == "GH-TOK"
    finally:
        con.close()


def test_url_derivation_only_when_registry_misses(tmp_path):
    """Registry stays first + exact: a search-index row WITH a registry match keeps the registry
    token even though its apply_url would derive a different one. URL derivation fires only for the
    registry-miss row."""
    path = tmp_path / "idx.sqlite"
    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        _insert(con, "reg", source="workday", company_key="acme", apply_url="x")
        _insert(con, "url", source="workday", company_key="nokey", apply_url="x")
        con.commit()

        fake = {"workday": lambda _u: "URL-TOK"}
        backfill_board_tokens(
            con,
            by_key={("acme", "workday"): "REG-TOK"},
            by_dom={},
            url_parsers=fake,
            search_index_sources={"workday"},
        )
        con.commit()
        assert _token(con, "reg") == "REG-TOK"  # registry preferred over URL
        assert _token(con, "url") == "URL-TOK"  # derived only where registry missed
    finally:
        con.close()


def test_url_derivation_tries_listing_url_and_ignores_garbage(tmp_path):
    """`apply_url` is tried first; a NULL/garbage apply_url falls back to `listing_url`; a row with
    no derivable URL at all stays NULL (never raises)."""
    path = tmp_path / "idx.sqlite"
    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        # apply_url is garbage (parser returns None) -> falls back to a real listing_url
        _insert(
            con, "fallback", source="smartrecruiters", company_key="nokey",
            apply_url="not-a-url",
            listing_url="https://jobs.smartrecruiters.com/AcmeCorp/744000012345678",
        )
        # neither URL derivable -> stays NULL
        _insert(con, "none", source="smartrecruiters", company_key="nokey", apply_url="garbage")
        con.commit()

        backfill_board_tokens(con, by_key={}, by_dom={})  # real parsers + real search-index set
        con.commit()
        assert _token(con, "fallback") == "AcmeCorp"  # listing_url fallback
        assert _token(con, "none") is None
    finally:
        con.close()


def test_url_derivation_end_to_end_per_source(tmp_path):
    """Integration through the real provider `matches` parsers + real SEARCH_INDEX_SOURCES set:
    each search-index source's representative apply_url derives the exact fetch-token format."""
    path = tmp_path / "idx.sqlite"
    fresh_db(path)
    con = connect(path)
    cases = {
        "workday": (
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA/Eng_JR1",
            "nvidia|wd5|NVIDIAExternalCareerSite",
        ),
        "oracle": (
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/3001",
            "eeho.fa.us2.oraclecloud.com|CX_1",
        ),
        "smartrecruiters": (
            "https://jobs.smartrecruiters.com/AcmeCorp/744000012345678",
            "AcmeCorp",
        ),
        "icims": (
            "https://careers-winco.icims.com/jobs/12345/store-associate/job",
            "careers-winco.icims.com",
        ),
    }
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        for source, (apply_url, _expected) in cases.items():
            _insert(con, source, source=source, company_key="nokey", apply_url=apply_url)
        con.commit()

        backfill_board_tokens(con, by_key={}, by_dom={})  # defaults: real parsers + freshness set
        con.commit()
        for source, (_url, expected) in cases.items():
            assert _token(con, source) == expected
    finally:
        con.close()


def test_url_token_sources_are_all_search_index_sources():
    """Safety invariant: every source with a URL parser is a freshness SEARCH-INDEX source (never a
    deterministic one), so the gate can never URL-derive a deterministic source."""
    parsers = _default_url_parsers()
    assert set(parsers) <= SEARCH_INDEX_SOURCES
    assert not (set(parsers) & DETERMINISTIC_SOURCES)


def test_derive_token_from_url_unit():
    """Unit-level per-source parse: `_derive_token_from_url` + each provider's `matches`."""
    parsers = _default_url_parsers()
    assert (
        _derive_token_from_url(
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/Eng_JR1",
            None,
            parsers["workday"],
        )
        == "nvidia|wd5|NVIDIAExternalCareerSite"
    )
    assert (
        _derive_token_from_url(
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2/job/9",
            None,
            parsers["oracle"],
        )
        == "eeho.fa.us2.oraclecloud.com|CX_2"
    )
    # None on unparseable input, and a raising parser is swallowed to None
    assert _derive_token_from_url("garbage", None, parsers["icims"]) is None
    assert _derive_token_from_url(None, None, parsers["workday"]) is None

    def _boom(_u):
        raise ValueError("boom")

    assert _derive_token_from_url("anything", None, _boom) is None
