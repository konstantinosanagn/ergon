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
    assert {
        "id",
        "sig",
        "fetched_at",
        "attempts",
        "snippet",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_interval",
        "years_min",
        "years_max",
        "degree_min",
        "degree_required",
        "sponsorship_offered",
    } <= cols
    # sig is stable + independent of the (to-be-fetched) description
    s1 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    s2 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    assert s1 == s2 and isinstance(s1, str)
    assert detail_sig({"content_hash": "xyz"}) != s1


def test_detailref_from_row():
    ref = DetailRef.from_row(
        {
            "id": "1",
            "source": "oracle",
            "board_token": "t",
            "apply_url": "http://x",
            "listing_url": None,
            "content_hash": "h",
        }
    )
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

    # NOTE: the REAL jobs schema has NO `description` column (discard-after-extract) — only `snippet`.
    # The reconcile pass selects candidates by EMPTY snippet, so the stub must mirror that: the 5th
    # tuple element is the row's snippet (None/'' => a Tier-3 candidate; non-empty => already has a JD).
    p = tmp_path / "index.sqlite"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
        "listing_url TEXT, content_hash TEXT, snippet TEXT, "
        "salary_min REAL, salary_max REAL, years_min INTEGER)"
    )
    c.executemany(
        "INSERT INTO jobs (id,source,apply_url,content_hash,snippet) VALUES (?,?,?,?,?)", rows
    )
    c.commit()
    c.close()
    return str(p)


def test_reconcile_fetches_missing_extracts_and_caps(tmp_path):
    idx = _mk_index(
        tmp_path, [(str(i), "oracle", f"http://x/{i}", f"h{i}", None) for i in range(5)]
    )
    det = str(tmp_path / "detail.sqlite")

    async def fake(ref):  # returns a JD with a parseable salary
        return f"<p>Great role. Salary: $120,000 - $150,000 / year. Req {ref.id}.</p>"

    stats = anyio.run(
        lambda: reconcile_detail_tier(
            det, idx, fetch_detail=fake, max_details=3, now=lambda: "2026-07-12T00:00:00Z"
        )
    )
    # capped at 3 of 5 fetched this run; `missing` is the REMAINING drainable backlog after the
    # pass (the 2 not reached), so it decreases toward 0 as the drain loop runs.
    assert stats["fetched"] == 3 and stats["missing"] == 2
    con = open_detail(det)
    got = con.execute(
        "SELECT salary_min, salary_max, snippet, fetched_at FROM job_detail"
    ).fetchall()
    assert len(got) == 3
    assert got[0][0] == 120000.0 and got[0][1] == 150000.0  # extracted, text discarded
    assert got[0][2] and len(got[0][2]) <= 300  # snippet kept
    assert got[0][3] == "2026-07-12T00:00:00Z"


def test_reconcile_selects_by_empty_snippet_not_description(tmp_path):
    # Regression: the candidate predicate must use the REAL `snippet` column, not a `description`
    # column (which does not exist on the real jobs schema). A row that already carries a snippet
    # (its JD is captured) must be skipped; only the empty-snippet row is fetched.
    idx = _mk_index(
        tmp_path,
        [
            ("empty", "smartrecruiters", "http://x/empty", "h1", None),
            ("has_jd", "smartrecruiters", "http://x/has", "h2", "Already has a real snippet."),
        ],
    )
    det = str(tmp_path / "detail.sqlite")
    fetched = []

    async def fake(ref):
        fetched.append(ref.id)
        return "<p>Salary: $100,000 / year</p>"

    stats = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    assert fetched == ["empty"]  # snippet-bearing row skipped, empty-snippet row fetched
    assert stats["fetched"] == 1 and stats["missing"] == 0


def test_reconcile_nonfatal_and_retry_budget(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")

    async def boom(ref):
        raise TimeoutError("dead page")

    s1 = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=boom, now=lambda: "t"))
    assert s1["failed"] == 1 and s1["fetched"] == 0
    con = open_detail(det)
    assert (
        con.execute("SELECT attempts FROM job_detail WHERE id='1'").fetchone()[0] == 1
    )  # counted, not fatal


def test_reconcile_sig_skips_unchanged(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")
    calls = []

    async def fake(ref):
        calls.append(ref.id)
        return "<p>Salary: $100,000 / year</p>"

    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    anyio.run(
        lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t")
    )  # 2nd run
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
            "source": "oracle",
            "company": "Acme",
            "remote": "unknown",
            "level": "mid",
            "employment_type": "full_time",
            "ts": "2026-07-01T00:00:00Z",
            "build_id": "b1",
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "salary_interval": None,
            "years_min": None,
            "years_max": None,
            "degree_min": None,
            "degree_required": None,
            "sponsorship_offered": None,
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
            "id": None,
            "sig": None,
            "fetched_at": "2026-07-01T00:00:00Z",
            "attempts": 0,
            "snippet": None,
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "salary_interval": None,
            "years_min": None,
            "years_max": None,
            "degree_min": None,
            "degree_required": None,
            "sponsorship_offered": None,
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
    idx = _mk_real_index(
        tmp_path,
        [
            {"id": "1", "content_hash": "h1", "title": "Engineer"},  # salary_min NULL, snippet NULL
        ],
    )
    det = _mk_detail_sidecar(
        tmp_path,
        [
            {
                "id": "1",
                "sig": good_sig,
                "salary_min": 90000.0,
                "salary_max": 120000.0,
                "salary_currency": "USD",
                "snippet": "Great role, remote-friendly.",
            },
        ],
    )
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
    idx = _mk_real_index(
        tmp_path,
        [
            {
                "id": "1",
                "content_hash": "h1",
                "title": "Engineer",
            },  # current sig differs from stale
        ],
    )
    det = _mk_detail_sidecar(
        tmp_path,
        [
            {"id": "1", "sig": stale_sig, "salary_min": 90000.0, "snippet": "Stale text."},
        ],
    )
    n = merge_detail_into_index(idx, det)
    assert n == 0
    row = idx.execute("SELECT salary_min, snippet FROM jobs WHERE id='1'").fetchone()
    assert row[0] is None and row[1] is None  # untouched -- material change, sidecar not applied


def test_merge_never_clobbers_a_value_the_list_crawl_provided(tmp_path):
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(
        tmp_path,
        [
            # list crawl already gave salary_min + a snippet; salary_max still NULL.
            {
                "id": "1",
                "content_hash": "h1",
                "title": "Engineer",
                "salary_min": 50000.0,
                "snippet": "Original list-crawl snippet.",
            },
        ],
    )
    det = _mk_detail_sidecar(
        tmp_path,
        [
            {
                "id": "1",
                "sig": good_sig,
                "salary_min": 999999.0,
                "salary_max": 130000.0,
                "snippet": "Sidecar snippet should not win.",
            },
        ],
    )
    n = merge_detail_into_index(idx, det)
    assert n == 1  # salary_max was filled, so this row did change
    row = idx.execute("SELECT salary_min, salary_max, snippet FROM jobs WHERE id='1'").fetchone()
    assert row[0] == 50000.0  # list-crawl value preserved, NOT clobbered
    assert row[1] == 130000.0  # NULL column filled from the sidecar
    assert row[2] == "Original list-crawl snippet."  # existing snippet preserved


def test_merge_guards_bad_int_casts_for_degree_and_sponsorship(tmp_path):
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(
        tmp_path,
        [
            {"id": "1", "content_hash": "h1", "title": "Engineer"},
        ],
    )
    det = str(tmp_path / "detail.sqlite")
    con = open_detail(det)
    # Bypass the normal INSERT path to inject a non-castable value directly (schema has no CHECK
    # on job_detail, unlike jobs -- this simulates corrupt/legacy sidecar data).
    con.execute(
        "INSERT INTO job_detail (id, sig, degree_required, sponsorship_offered, salary_min) "
        "VALUES ('1', ?, 'not-an-int', 1, 75000.0)",
        (good_sig,),
    )
    con.commit()
    con.close()
    n = merge_detail_into_index(idx, det)
    assert n == 1  # salary_min + sponsorship_offered still applied
    row = idx.execute(
        "SELECT salary_min, degree_required, sponsorship_offered FROM jobs WHERE id='1'"
    ).fetchone()
    assert row[0] == 75000.0
    assert row[1] is None  # bad cast guarded -- column left untouched, no crash
    assert row[2] == 1


def test_merge_is_per_row_atomic_check_violation_does_not_discard_good_merges(tmp_path):
    """Reproduces the reviewer-found bug: a single sidecar row whose merged values trip a
    DB-level CHECK (salary_min <= salary_max) must not sink an EARLIER, already-clean merge in
    the same call -- the whole point of Task 4's durability guarantee. Row 'A' merges cleanly;
    row 'B' has an existing salary_min=90000 with the sidecar filling salary_max=50000 (NULL-guard
    still applies since salary_max was NULL on the index row), tripping the CHECK."""
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx_path = tmp_path / "real_index.sqlite"
    idx = _mk_real_index(
        tmp_path,
        [
            {"id": "A", "content_hash": "h1", "title": "Engineer"},  # clean target
            {
                "id": "B",
                "content_hash": "h1",
                "title": "Engineer",
                "salary_min": 90000.0,
            },  # CHECK trap
        ],
    )
    det = _mk_detail_sidecar(
        tmp_path,
        [
            {"id": "A", "sig": good_sig, "salary_min": 60000.0, "salary_max": 80000.0},
            {
                "id": "B",
                "sig": good_sig,
                "salary_max": 50000.0,
            },  # 90000 <= 50000 violates the CHECK
        ],
    )

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


def test_reconcile_prefers_structured_detailfetch_salary_over_body(tmp_path):
    # A provider that returns DetailFetch(text, salary) must have its STRUCTURED salary persisted,
    # even when the text body carries a DIFFERENT parseable figure -- the structured range wins
    # (enrich only fills a still-empty field, and the reconcile seeds it first). Proves the whole
    # str|DetailFetch plumbing end to end.
    from ergon_tracker.models import DetailFetch, Salary, SalaryInterval

    idx = _mk_index(tmp_path, [("1", "rippling", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")

    async def fake(ref):
        return DetailFetch(
            text="<p>Decoy in body: Salary $10,000 - $20,000 / year.</p>",
            salary=Salary(
                min_amount=55000, max_amount=65000, currency="USD", interval=SalaryInterval.YEAR
            ),
        )

    stats = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    assert stats["fetched"] == 1
    con = open_detail(det)
    row = con.execute(
        "SELECT salary_min, salary_max, salary_currency, salary_interval, snippet FROM job_detail"
    ).fetchone()
    assert row[0] == 55000.0 and row[1] == 65000.0  # structured, NOT the 10k-20k body decoy
    assert row[2] == "USD" and row[3] == "year"
    assert row[4] and "Decoy" in row[4]  # snippet still comes from the text body


def test_reconcile_detailfetch_without_salary_falls_back_to_body(tmp_path):
    # DetailFetch(salary=None) must behave exactly like a bare str: the body extractor fills salary.
    from ergon_tracker.models import DetailFetch

    idx = _mk_index(tmp_path, [("1", "rippling", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")

    async def fake(ref):
        return DetailFetch(text="<p>Pay: $120,000 - $150,000 per year.</p>", salary=None)

    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    con = open_detail(det)
    row = con.execute("SELECT salary_min, salary_max FROM job_detail").fetchone()
    assert row[0] == 120000.0 and row[1] == 150000.0  # body-extracted, as before


def test_reconcile_recovers_structured_location_and_merges_country(tmp_path):
    # A provider returning DetailFetch(locations=...) must persist city/country to the sidecar, and
    # merge must fill the index row's NULL country -- the fix for "N Locations" placeholder / empty
    # list-scrape geo. Also exercises the v1->v2 sidecar column migration implicitly (fresh db).
    import sqlite3

    from ergon_tracker.models import DetailFetch, Location

    # index row with a placeholder location and NULL city/country (the Arcus/jobvite case)
    idx = tmp_path / "index.sqlite"
    c = sqlite3.connect(idx)
    c.execute(
        "CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
        "listing_url TEXT, content_hash TEXT, title TEXT, level TEXT, snippet TEXT, "
        "salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT, "
        "years_min INTEGER, years_max INTEGER, degree_min TEXT, degree_required INTEGER, "
        "sponsorship_offered INTEGER, city TEXT, country TEXT, location TEXT)"
    )
    c.execute(
        "INSERT INTO jobs (id,source,apply_url,content_hash,title,level,snippet,location) "
        "VALUES ('1','jobvite','http://x/1','h1','Eng','unknown',NULL,'3 Locations')"
    )
    c.commit()
    c.close()
    det = str(tmp_path / "detail.sqlite")

    async def fake(ref):
        return DetailFetch(
            text="<p>Great role.</p>",
            locations=[
                Location(
                    raw="Brisbane, California, United States",
                    city="Brisbane",
                    region="California",
                    country="United States",
                )
            ],
        )

    anyio.run(lambda: reconcile_detail_tier(det, str(idx), fetch_detail=fake, now=lambda: "t"))
    dcon = open_detail(det)
    srow = dcon.execute("SELECT city, country FROM job_detail WHERE id='1'").fetchone()
    assert srow == ("Brisbane", "United States")  # persisted to the sidecar

    icon = sqlite3.connect(idx)
    merge_detail_into_index(icon, det)
    got = icon.execute("SELECT city, country, location FROM jobs WHERE id='1'").fetchone()
    icon.close()
    assert got[0] == "Brisbane" and got[1] == "United States"  # NULL city/country filled
    assert got[2] == "3 Locations"  # raw location left as-is (never clobbered)


def test_ensure_detail_schema_migrates_v1_sidecar_adds_city_country(tmp_path):
    # A pre-existing v1 sidecar (no city/country) must gain the columns without data loss.
    import sqlite3

    from ergon_tracker.index.detail import ensure_detail_schema

    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE job_detail (id TEXT PRIMARY KEY, sig TEXT, fetched_at TEXT, "
        "attempts INTEGER, snippet TEXT, salary_min REAL)"
    )
    c.execute("INSERT INTO job_detail (id, snippet) VALUES ('a', 'kept')")
    c.commit()
    ensure_detail_schema(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(job_detail)")}
    assert "city" in cols and "country" in cols
    assert c.execute("SELECT snippet FROM job_detail WHERE id='a'").fetchone()[0] == "kept"
    c.close()


# --- location backfill (opt-in drain re-fetch of already-drained, location-less rows) -----------
#
# The 5 non-Workday location-capable sources (oracle/successfactors/eightfold/radancy/rippling +
# bamboohr/jobvite) gained structured-location wiring AFTER much of their backlog was already
# drained. Those drained rows have a snippet, so the normal empty-snippet Tier-3 selection can never
# reach them again. The backfill mode widens the selection to the union of (empty-snippet) and
# (location-capable + NULL-location) rows and re-queues the drained ones so a re-fetch can recover
# their city/country -- WITHOUT losing any already-recovered field on a failed re-fetch.


def _mk_index_loc(tmp_path, rows):
    """Index stub WITH city/country columns (the real jobs schema has them), for backfill tests.
    Each row tuple: (id, source, apply_url, content_hash, snippet, city, country)."""
    import sqlite3

    p = tmp_path / "index.sqlite"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
        "listing_url TEXT, content_hash TEXT, snippet TEXT, city TEXT, country TEXT, "
        "salary_min REAL, salary_max REAL, years_min INTEGER)"
    )
    c.executemany(
        "INSERT INTO jobs (id,source,apply_url,content_hash,snippet,city,country) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    c.commit()
    c.close()
    return str(p)


def _seed_drained(det_path, *, id_, content_hash, salary_min, fetched_at="2026-07-01T00:00:00Z"):
    """Seed the sidecar with an ALREADY-DRAINED row: sig current, fetched_at set, a recovered salary,
    but NO location (mirrors a row drained before its provider's location wiring existed)."""
    con = open_detail(det_path)
    con.execute(
        "INSERT INTO job_detail(id, sig, fetched_at, attempts, snippet, salary_min) "
        "VALUES (?,?,?,0,?,?)",
        (id_, detail_sig({"content_hash": content_hash}), fetched_at, "old snippet", salary_min),
    )
    con.commit()
    con.close()


def _loc_fetch(with_salary=True):
    """fetch_detail stub returning a DetailFetch that carries a structured location (and, by default,
    a re-parseable salary in the JD body). NEVER embeds/network -- pure in-memory."""
    from ergon_tracker.models import DetailFetch, Location

    async def fake(ref):
        body = "Senior Engineer in New York. "
        if with_salary:
            body += "Salary: $120,000 - $150,000 / year. "
        return DetailFetch(
            text=f"<p>{body}Req {ref.id}.</p>",
            locations=[Location(city="New York", country="US", raw="New York, NY, United States")],
        )

    return fake


def test_location_backfill_requeues_drained_row_and_fills_location(tmp_path):
    # A: oracle, drained (has snippet), NO location  -> the backfill target
    # B: oracle, drained, ALREADY has a location     -> must never be re-fetched
    # C: greenhouse (not location-capable), drained   -> out of scope for backfill
    idx = _mk_index_loc(
        tmp_path,
        [
            ("A", "oracle", "http://x/A", "ha", "drained A", None, None),
            ("B", "oracle", "http://x/B", "hb", "drained B", "London", "GB"),
            ("C", "greenhouse", "http://x/C", "hc", "drained C", None, None),
        ],
    )
    det = str(tmp_path / "detail.sqlite")
    _seed_drained(det, id_="A", content_hash="ha", salary_min=100000.0)
    stats = anyio.run(
        lambda: reconcile_detail_tier(
            det,
            idx,
            fetch_detail=_loc_fetch(),
            max_details=50,
            sources=["oracle", "greenhouse"],
            now=lambda: "2026-07-15T00:00:00Z",
            location_backfill=True,
        )
    )
    assert stats["location_requeued"] == 1  # only A (B has a location, C is not location-capable)
    assert stats["fetched"] == 1  # only A re-fetched
    con = open_detail(det)
    city, country, sal, fetched = con.execute(
        "SELECT city, country, salary_min, fetched_at FROM job_detail WHERE id='A'"
    ).fetchone()
    assert city and country  # location now recovered
    assert sal == 120000.0  # re-extracted from the (present) JD salary on success
    assert fetched == "2026-07-15T00:00:00Z"
    # B and C were never selected -> no sidecar rows created for them.
    assert con.execute("SELECT COUNT(*) FROM job_detail WHERE id IN ('B','C')").fetchone()[0] == 0
    con.close()
    # Idempotent: a second backfill re-queues nothing (A now carries a location in the sidecar).
    stats2 = anyio.run(
        lambda: reconcile_detail_tier(
            det,
            idx,
            fetch_detail=_loc_fetch(),
            max_details=50,
            sources=["oracle", "greenhouse"],
            now=lambda: "2026-07-15T01:00:00Z",
            location_backfill=True,
        )
    )
    assert stats2["location_requeued"] == 0 and stats2["fetched"] == 0


def test_location_backfill_preserves_salary_on_failed_refetch(tmp_path):
    # The no-worse-than-current guarantee: a FAILED re-fetch must never drop the already-recovered
    # salary -- _record_attempt only bumps attempts.
    idx = _mk_index_loc(tmp_path, [("A", "oracle", "http://x/A", "ha", "drained A", None, None)])
    det = str(tmp_path / "detail.sqlite")
    _seed_drained(det, id_="A", content_hash="ha", salary_min=100000.0)

    async def boom(ref):
        raise RuntimeError("network down")

    stats = anyio.run(
        lambda: reconcile_detail_tier(
            det,
            idx,
            fetch_detail=boom,
            max_details=50,
            sources=["oracle"],
            now=lambda: "2026-07-15T00:00:00Z",
            location_backfill=True,
        )
    )
    assert stats["location_requeued"] == 1 and stats["failed"] == 1 and stats["fetched"] == 0
    con = open_detail(det)
    city, country, sal, fetched, attempts = con.execute(
        "SELECT city, country, salary_min, fetched_at, attempts FROM job_detail WHERE id='A'"
    ).fetchone()
    assert sal == 100000.0  # preserved -- not clobbered to NULL
    assert city is None and country is None  # still no location (fetch failed)
    assert fetched is None and attempts == 1  # re-queued then one spent attempt
    con.close()


def test_location_backfill_union_covers_empty_snippet_rows(tmp_path):
    # The union arm: a backfill run still drains the ordinary empty-snippet backlog too (so a
    # drained-but-not-yet-merged row's carry-forward is never pruned away). E is a normal Tier-3
    # candidate; A is a drained backfill target -- both must be fetched in one pass.
    idx = _mk_index_loc(
        tmp_path,
        [
            ("E", "greenhouse", "http://x/E", "he", None, None, None),
            ("A", "oracle", "http://x/A", "ha", "drained A", None, None),
        ],
    )
    det = str(tmp_path / "detail.sqlite")
    _seed_drained(det, id_="A", content_hash="ha", salary_min=100000.0)
    stats = anyio.run(
        lambda: reconcile_detail_tier(
            det,
            idx,
            fetch_detail=_loc_fetch(),
            max_details=50,
            sources=["oracle", "greenhouse"],
            now=lambda: "2026-07-15T00:00:00Z",
            location_backfill=True,
        )
    )
    assert stats["fetched"] == 2  # BOTH the empty-snippet row and the drained target
    assert stats["location_requeued"] == 1  # only A (E had no prior sidecar row)


def test_location_backfill_off_leaves_drained_rows_untouched(tmp_path):
    # Default (off): a drained row keeps its snippet, so it is not a normal Tier-3 candidate and is
    # never touched -- the ordinary daily/drain path is byte-for-byte unchanged.
    idx = _mk_index_loc(tmp_path, [("A", "oracle", "http://x/A", "ha", "drained A", None, None)])
    det = str(tmp_path / "detail.sqlite")
    _seed_drained(det, id_="A", content_hash="ha", salary_min=100000.0)
    stats = anyio.run(
        lambda: reconcile_detail_tier(
            det,
            idx,
            fetch_detail=_loc_fetch(),
            max_details=50,
            sources=["oracle"],
            now=lambda: "2026-07-15T00:00:00Z",
        )
    )  # location_backfill defaults False
    assert stats["fetched"] == 0
    assert stats.get("location_requeued", 0) == 0
    con = open_detail(det)
    city, sal, fetched = con.execute(
        "SELECT city, salary_min, fetched_at FROM job_detail WHERE id='A'"
    ).fetchone()
    assert city is None and sal == 100000.0 and fetched == "2026-07-01T00:00:00Z"
    con.close()


# --- RETRY_CAP engagement: dead rows must STOP being re-fetched (the sig-persistence fix) ---------
#
# BUG (fixed): _record_attempt wrote only (id, attempts), never `sig`, so a never-succeeded row kept
# sig=NULL. _eligible's `d["sig"] != sig` guard then short-circuited to True EVERY run (NULL never
# equals a real content_sig), so the `attempts < RETRY_CAP` gate was never reached and dead/expired
# postings were re-fetched forever (a shard re-attempted the identical ~5.5k dead rows every run).
# Persisting the sig lets a persistently-dead row reach attempts>=RETRY_CAP and be abandoned; a
# genuinely-changed posting (new sig) still re-qualifies with a fresh budget.


def _ref(id_, sig, source="oracle"):
    return DetailRef(
        id=id_,
        source=source,
        token=None,
        apply_url=f"http://x/{id_}",
        listing_url=None,
        content_sig=sig,
    )


def test_record_attempt_persists_sig_and_increments(tmp_path):
    from ergon_tracker.index.detail import _record_attempt

    con = open_detail(str(tmp_path / "d.sqlite"))
    _record_attempt(con, _ref("x", "SIGA"))
    con.commit()
    sig, att = con.execute("SELECT sig, attempts FROM job_detail WHERE id='x'").fetchone()
    assert sig == "SIGA" and att == 1  # sig written (was NULL before the fix)
    _record_attempt(con, _ref("x", "SIGA"))
    _record_attempt(con, _ref("x", "SIGA"))
    con.commit()
    assert con.execute("SELECT attempts FROM job_detail WHERE id='x'").fetchone()[0] == 3
    con.close()


def test_record_attempt_resets_budget_when_posting_changes(tmp_path):
    from ergon_tracker.index.detail import RETRY_CAP, _eligible, _load_existing, _record_attempt

    con = open_detail(str(tmp_path / "d.sqlite"))
    for _ in range(RETRY_CAP):
        _record_attempt(con, _ref("x", "SIG1"))
    con.commit()
    ex = _load_existing(con)
    assert not _eligible("x", "SIG1", ex)  # capped: same sig, attempts == RETRY_CAP -> skip
    assert _eligible("x", "SIG2", ex)  # posting changed -> eligible again
    _record_attempt(con, _ref("x", "SIG2"))
    con.commit()
    sig, att = con.execute("SELECT sig, attempts FROM job_detail WHERE id='x'").fetchone()
    assert sig == "SIG2" and att == 1  # fresh budget for the changed posting (reset to 1)
    con.close()


def test_eligible_reaches_retry_cap_gate_once_sig_persisted(tmp_path):
    # Directly exercise the gate: a failed row with a MATCHING persisted sig is skipped at the cap.
    from ergon_tracker.index.detail import RETRY_CAP, _eligible, _load_existing, _record_attempt

    con = open_detail(str(tmp_path / "d.sqlite"))
    for _ in range(RETRY_CAP - 1):
        _record_attempt(con, _ref("x", "SIG"))
        con.commit()
        assert _eligible("x", "SIG", _load_existing(con))  # still under cap -> eligible
    _record_attempt(con, _ref("x", "SIG"))
    con.commit()
    assert not _eligible("x", "SIG", _load_existing(con))  # hit cap -> abandoned
    con.close()


def test_dead_row_abandoned_after_retry_cap_across_runs(tmp_path):
    # THE regression/stress test: a row whose fetch ALWAYS fails must be attempted exactly RETRY_CAP
    # times across successive reconcile passes, then permanently skipped -- NOT re-fetched every run
    # (the pre-fix behaviour that burned ~96% of the finisher drain on dead rows).
    from ergon_tracker.index.detail import RETRY_CAP

    idx = _mk_index(tmp_path, [("dead", "oracle", "http://x/dead", "h", None)])
    det = str(tmp_path / "detail.sqlite")
    calls = []

    async def always_fail(ref):
        calls.append(ref.id)
        return None  # dead posting -> no description -> _record_attempt

    for _ in range(RETRY_CAP + 3):  # run several extra passes past the cap
        anyio.run(
            lambda: reconcile_detail_tier(
                det,
                idx,
                fetch_detail=always_fail,
                max_details=10,
                now=lambda: "2026-07-15T00:00:00Z",
            )
        )
    assert (
        len(calls) == RETRY_CAP
    )  # attempted RETRY_CAP times then abandoned (pre-fix: RETRY_CAP+3)
