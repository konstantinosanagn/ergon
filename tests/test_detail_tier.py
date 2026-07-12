import sqlite3

import anyio

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.detail import (
    DetailRef,
    detail_sig,
    ensure_detail_schema,
    merge_detail_into_index,
    open_detail,
    reconcile_detail_tier,
)


def test_schema_and_sig():
    con = sqlite3.connect(":memory:")
    ensure_detail_schema(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(job_detail)")}
    assert {"id", "sig", "fetched_at", "attempts", "snippet",
            "salary_min", "salary_max", "salary_currency", "salary_interval",
            "years_min", "years_max", "degree_min", "degree_required",
            "sponsorship_offered"} <= cols
    # sig is stable + independent of the (to-be-fetched) description
    s1 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    s2 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    assert s1 == s2 and isinstance(s1, str)
    assert detail_sig({"content_hash": "xyz"}) != s1

def test_detailref_from_row():
    ref = DetailRef.from_row({"id": "1", "source": "oracle", "board_token": "t",
                              "apply_url": "http://x", "listing_url": None, "content_hash": "h"})
    assert ref.id == "1" and ref.source == "oracle" and ref.apply_url == "http://x"

def test_sig_fallback_without_content_hash():
    # No content_hash -> falls back to "title|level"; stable across calls; still a valid sig.
    s1 = detail_sig({"title": "Software Engineer", "level": "senior"})
    s2 = detail_sig({"title": "Software Engineer", "level": "senior"})
    assert s1 == s2 and isinstance(s1, str) and s1
    # Changing title or level changes the fallback sig.
    assert detail_sig({"title": "Software Engineer", "level": "junior"}) != s1
    assert detail_sig({"title": "Data Scientist", "level": "senior"}) != s1
    # A present-but-falsy content_hash ("") still falls back rather than hashing "".
    assert detail_sig({"content_hash": "", "title": "Software Engineer", "level": "senior"}) == s1


def _mk_index(tmp_path, rows):
    import sqlite3

    p = tmp_path / "index.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
              "listing_url TEXT, content_hash TEXT, description TEXT, snippet TEXT, "
              "salary_min REAL, salary_max REAL, years_min INTEGER)")
    c.executemany("INSERT INTO jobs (id,source,apply_url,content_hash,description) VALUES (?,?,?,?,?)",
                  rows)
    c.commit()
    c.close()
    return str(p)

def test_reconcile_fetches_missing_extracts_and_caps(tmp_path):
    idx = _mk_index(tmp_path, [(str(i), "oracle", f"http://x/{i}", f"h{i}", None) for i in range(5)])
    det = str(tmp_path / "detail.sqlite")
    async def fake(ref):  # returns a JD with a parseable salary
        return f"<p>Great role. Salary: $120,000 - $150,000 / year. Req {ref.id}.</p>"
    stats = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, max_details=3,
                                                    now=lambda: "2026-07-12T00:00:00Z"))
    # capped at 3 of 5 fetched this run; `missing` is the REMAINING drainable backlog after the
    # pass (the 2 not reached), so it decreases toward 0 as the drain loop runs.
    assert stats["fetched"] == 3 and stats["missing"] == 2
    con = open_detail(det)
    got = con.execute("SELECT salary_min, salary_max, snippet, fetched_at FROM job_detail").fetchall()
    assert len(got) == 3
    assert got[0][0] == 120000.0 and got[0][1] == 150000.0   # extracted, text discarded
    assert got[0][2] and len(got[0][2]) <= 300               # snippet kept
    assert got[0][3] == "2026-07-12T00:00:00Z"

def test_reconcile_nonfatal_and_retry_budget(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")
    async def boom(ref): raise TimeoutError("dead page")
    s1 = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=boom, now=lambda: "t"))
    assert s1["failed"] == 1 and s1["fetched"] == 0
    con = open_detail(det)
    assert con.execute("SELECT attempts FROM job_detail WHERE id='1'").fetchone()[0] == 1  # counted, not fatal

def test_reconcile_sig_skips_unchanged(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")
    calls = []
    async def fake(ref):
        calls.append(ref.id)
        return "<p>Salary: $100,000 / year</p>"
    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))  # 2nd run
    assert calls == ["1"]  # unchanged sig -> not re-fetched


# --- build merge (Task 4): real index schema (db.py/schema.sql), not the reconcile-pass stub ---

def _mk_real_index(tmp_path, job_rows):
    """Build an index DB against the REAL production `jobs` schema (schema.sql via db.fresh_db),
    so the merge is proven against the actual column set/constraints, not a test-only stand-in."""
    p = tmp_path / "real_index.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    for row in job_rows:
        defaults = {
            "source": "oracle", "company": "Acme", "remote": "unknown", "level": "mid",
            "employment_type": "full_time", "ts": "2026-07-01T00:00:00Z", "build_id": "b1",
            "salary_min": None, "salary_max": None, "salary_currency": None,
            "salary_interval": None, "years_min": None, "years_max": None,
            "degree_min": None, "degree_required": None, "sponsorship_offered": None,
            "snippet": None,
        }
        defaults.update(row)
        con.execute(
            "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
            "employment_type, status, first_seen, last_seen, fetched_at, build_id, "
            "salary_min, salary_max, salary_currency, salary_interval, years_min, years_max, "
            "degree_min, degree_required, sponsorship_offered, snippet) "
            "VALUES (:id, :content_hash, :source, :company, :title, :remote, :level, "
            ":employment_type, 'active', :ts, :ts, :ts, :build_id, "
            ":salary_min, :salary_max, :salary_currency, :salary_interval, :years_min, "
            ":years_max, :degree_min, :degree_required, :sponsorship_offered, :snippet)",
            defaults,
        )
    con.commit()
    return con


def _mk_detail_sidecar(tmp_path, rows):
    """rows: list of dicts with at least id, sig; other job_detail columns default to None."""
    p = tmp_path / "detail.sqlite"
    con = open_detail(str(p))
    for row in rows:
        defaults = {
            "id": None, "sig": None, "fetched_at": "2026-07-01T00:00:00Z", "attempts": 0,
            "snippet": None, "salary_min": None, "salary_max": None, "salary_currency": None,
            "salary_interval": None, "years_min": None, "years_max": None,
            "degree_min": None, "degree_required": None, "sponsorship_offered": None,
        }
        defaults.update(row)
        con.execute(
            "INSERT INTO job_detail (id, sig, fetched_at, attempts, snippet, salary_min, "
            "salary_max, salary_currency, salary_interval, years_min, years_max, degree_min, "
            "degree_required, sponsorship_offered) "
            "VALUES (:id, :sig, :fetched_at, :attempts, :snippet, :salary_min, :salary_max, "
            ":salary_currency, :salary_interval, :years_min, :years_max, :degree_min, "
            ":degree_required, :sponsorship_offered)",
            defaults,
        )
    con.commit()
    con.close()
    return str(p)


def test_merge_applies_recovered_fields_when_sig_matches(tmp_path):
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(tmp_path, [
        {"id": "1", "content_hash": "h1", "title": "Engineer"},  # salary_min NULL, snippet NULL
    ])
    det = _mk_detail_sidecar(tmp_path, [
        {"id": "1", "sig": good_sig, "salary_min": 90000.0, "salary_max": 120000.0,
         "salary_currency": "USD", "snippet": "Great role, remote-friendly."},
    ])
    n = merge_detail_into_index(idx, det)
    assert n == 1
    row = idx.execute(
        "SELECT salary_min, salary_max, salary_currency, snippet FROM jobs WHERE id='1'"
    ).fetchone()
    assert row[0] == 90000.0 and row[1] == 120000.0
    assert row[2] == "USD"
    assert row[3] == "Great role, remote-friendly."


def test_merge_skips_when_sig_does_not_match(tmp_path):
    stale_sig = detail_sig({"content_hash": "OLD-HASH", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(tmp_path, [
        {"id": "1", "content_hash": "h1", "title": "Engineer"},  # current sig differs from stale
    ])
    det = _mk_detail_sidecar(tmp_path, [
        {"id": "1", "sig": stale_sig, "salary_min": 90000.0, "snippet": "Stale text."},
    ])
    n = merge_detail_into_index(idx, det)
    assert n == 0
    row = idx.execute("SELECT salary_min, snippet FROM jobs WHERE id='1'").fetchone()
    assert row[0] is None and row[1] is None  # untouched -- material change, sidecar not applied


def test_merge_never_clobbers_a_value_the_list_crawl_provided(tmp_path):
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(tmp_path, [
        # list crawl already gave salary_min + a snippet; salary_max still NULL.
        {"id": "1", "content_hash": "h1", "title": "Engineer",
         "salary_min": 50000.0, "snippet": "Original list-crawl snippet."},
    ])
    det = _mk_detail_sidecar(tmp_path, [
        {"id": "1", "sig": good_sig, "salary_min": 999999.0, "salary_max": 130000.0,
         "snippet": "Sidecar snippet should not win."},
    ])
    n = merge_detail_into_index(idx, det)
    assert n == 1  # salary_max was filled, so this row did change
    row = idx.execute("SELECT salary_min, salary_max, snippet FROM jobs WHERE id='1'").fetchone()
    assert row[0] == 50000.0        # list-crawl value preserved, NOT clobbered
    assert row[1] == 130000.0       # NULL column filled from the sidecar
    assert row[2] == "Original list-crawl snippet."  # existing snippet preserved


def test_merge_guards_bad_int_casts_for_degree_and_sponsorship(tmp_path):
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(tmp_path, [
        {"id": "1", "content_hash": "h1", "title": "Engineer"},
    ])
    det = str(tmp_path / "detail.sqlite")
    con = open_detail(det)
    # Bypass the normal INSERT path to inject a non-castable value directly (schema has no CHECK
    # on job_detail, unlike jobs -- this simulates corrupt/legacy sidecar data).
    con.execute(
        "INSERT INTO job_detail (id, sig, degree_required, sponsorship_offered, salary_min) "
        "VALUES ('1', ?, 'not-an-int', 1, 75000.0)", (good_sig,),
    )
    con.commit()
    con.close()
    n = merge_detail_into_index(idx, det)
    assert n == 1  # salary_min + sponsorship_offered still applied
    row = idx.execute(
        "SELECT salary_min, degree_required, sponsorship_offered FROM jobs WHERE id='1'"
    ).fetchone()
    assert row[0] == 75000.0
    assert row[1] is None       # bad cast guarded -- column left untouched, no crash
    assert row[2] == 1


def test_merge_is_per_row_atomic_check_violation_does_not_discard_good_merges(tmp_path):
    """Reproduces the reviewer-found bug: a single sidecar row whose merged values trip a
    DB-level CHECK (salary_min <= salary_max) must not sink an EARLIER, already-clean merge in
    the same call -- the whole point of Task 4's durability guarantee. Row 'A' merges cleanly;
    row 'B' has an existing salary_min=90000 with the sidecar filling salary_max=50000 (NULL-guard
    still applies since salary_max was NULL on the index row), tripping the CHECK."""
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx_path = tmp_path / "real_index.sqlite"
    idx = _mk_real_index(tmp_path, [
        {"id": "A", "content_hash": "h1", "title": "Engineer"},                       # clean target
        {"id": "B", "content_hash": "h1", "title": "Engineer", "salary_min": 90000.0},  # CHECK trap
    ])
    det = _mk_detail_sidecar(tmp_path, [
        {"id": "A", "sig": good_sig, "salary_min": 60000.0, "salary_max": 80000.0},
        {"id": "B", "sig": good_sig, "salary_max": 50000.0},  # 90000 <= 50000 violates the CHECK
    ])

    n = merge_detail_into_index(idx, det)  # must not raise
    assert n == 1  # only A's merge counted; B was skipped, not applied

    rowA = idx.execute("SELECT salary_min, salary_max FROM jobs WHERE id='A'").fetchone()
    assert rowA[0] == 60000.0 and rowA[1] == 80000.0  # A's merge WAS applied

    rowB = idx.execute("SELECT salary_min, salary_max FROM jobs WHERE id='B'").fetchone()
    assert rowB[0] == 90000.0 and rowB[1] is None  # B left completely untouched

    # Reconnect fresh to prove A's merge actually persisted (single final commit fast path),
    # not just visible within the same still-open, possibly-uncommitted connection.
    idx.close()
    idx2 = sqlite3.connect(str(idx_path))
    persisted = idx2.execute("SELECT salary_min, salary_max FROM jobs WHERE id='A'").fetchone()
    idx2.close()
    assert persisted == (60000.0, 80000.0)
