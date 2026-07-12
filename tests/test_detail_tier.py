import sqlite3

import anyio

from ergon_tracker.index.detail import (
    DetailRef,
    detail_sig,
    ensure_detail_schema,
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
    p = tmp_path / "index.sqlite"; c = sqlite3.connect(p)
    c.execute("CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
              "listing_url TEXT, content_hash TEXT, description TEXT, snippet TEXT, "
              "salary_min REAL, salary_max REAL, years_min INTEGER)")
    c.executemany("INSERT INTO jobs (id,source,apply_url,content_hash,description) VALUES (?,?,?,?,?)",
                  rows); c.commit(); c.close(); return str(p)

def test_reconcile_fetches_missing_extracts_and_caps(tmp_path):
    idx = _mk_index(tmp_path, [(str(i), "oracle", f"http://x/{i}", f"h{i}", None) for i in range(5)])
    det = str(tmp_path / "detail.sqlite")
    async def fake(ref):  # returns a JD with a parseable salary
        return f"<p>Great role. Salary: $120,000 - $150,000 / year. Req {ref.id}.</p>"
    stats = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, max_details=3,
                                                    now=lambda: "2026-07-12T00:00:00Z"))
    assert stats["fetched"] == 3 and stats["missing"] == 5   # capped at 3 of 5
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
    async def fake(ref): calls.append(ref.id); return "<p>Salary: $100,000 / year</p>"
    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))  # 2nd run
    assert calls == ["1"]  # unchanged sig -> not re-fetched
