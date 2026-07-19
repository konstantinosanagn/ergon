"""``backfill_board_tokens`` fills jobs.board_token for carried-forward active rows (NULL until the
crawl re-visits the board) from the seed registry, so the freshness sweep covers ~all boards
immediately instead of ramping over the ~5-day crawl cycle. Registry token == the crawl's fetch
token (exact by construction); the backfill never overwrites an existing token and only touches
active rows. Registry maps are injected here for a deterministic offline test."""

from __future__ import annotations

from ergon_tracker.index.build import backfill_board_tokens
from ergon_tracker.index.db import connect, fresh_db


def _insert(con, jid, *, source, company_key, domain=None, board_token=None, status="active"):
    con.execute(
        "INSERT INTO jobs(id, content_hash, company_key, source, company, company_domain, title, "
        "remote, level, employment_type, board_token, status, first_seen, last_seen, fetched_at, "
        "build_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            jid, f"h-{jid}", company_key, source, "Co", domain, "Engineer",
            "unknown", "mid", "fulltime", board_token, status, "t", "t", "t", "b",
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
