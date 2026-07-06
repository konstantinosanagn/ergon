"""Schema-v2 prev-attach regressions: a prev index built on an OLDER schema (missing the
degree_min/degree_required columns added in schema v2) must still (a) carry forward and (b) diff for
the delta — the daily index froze 2026-07-05 when carry_forward's fixed-column SELECT hit "no such
column" and dropped the whole backlog, and the follow-up build (2026-07-06) then crashed post-publish
in build_delta's "p.degree_min IS c.degree_min" change test. Both now intersect with prev's columns.
"""

from __future__ import annotations

import re

from ergon_tracker.index.build import build_delta, carry_forward
from ergon_tracker.index.db import _schema_sql, connect, fresh_db

_N = 200


def _older_schema_sql() -> str:
    """Current schema with the two schema-v2 degree columns (and their index) stripped out."""
    sql = _schema_sql()
    sql = re.sub(r"\n\s*degree_min TEXT CHECK \(.*?\)\),", "", sql)
    sql = re.sub(r"\n\s*degree_required INTEGER CHECK \(.*?\)\),", "", sql)
    sql = re.sub(r"\nCREATE INDEX idx_jobs_degree [^;]*;", "", sql)
    return sql


def _build_prev(path) -> None:
    """A prev index on the OLDER schema, with a parent company + N jobs + a source each."""
    con = connect(path)
    try:
        con.executescript(_older_schema_sql())
        assert "degree_min" not in {
            r[1] for r in con.execute("PRAGMA table_info(jobs)")
        }, "prev must lack degree_min to reproduce the schema-v2 drift"
        con.execute(
            "INSERT INTO companies(company_key, display_name) VALUES('acme', 'Acme')"
        )
        con.executemany(
            "INSERT INTO jobs(id, content_hash, company_key, source, company, title, remote, "
            "level, employment_type, first_seen, last_seen, fetched_at, build_id) "
            "VALUES(?, ?, 'acme', 'greenhouse', 'Acme', ?, 'unknown', 'mid', 'fulltime', "
            "'2026-07-01', '2026-07-01', '2026-07-01', 'b0')",
            [(f"job-{i}", f"h{i}", f"Engineer {i}") for i in range(_N)],
        )
        con.executemany(
            "INSERT INTO job_sources(job_id, source, source_job_id, fetched_at) "
            "VALUES(?, 'greenhouse', ?, '2026-07-01')",
            [(f"job-{i}", f"src-{i}") for i in range(_N)],
        )
        con.commit()
    finally:
        con.close()


def test_carry_forward_older_schema_prev(tmp_path):
    prev = tmp_path / "prev.sqlite"
    _build_prev(prev)

    out = tmp_path / "out.sqlite"
    fresh_db(out)
    con = connect(out)
    try:
        con.execute("INSERT INTO companies(company_key, display_name) VALUES('acme', 'Acme')")
        con.commit()
        carried = carry_forward(con, prev, crawled_keys=set())

        assert carried == _N
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == _N
        assert con.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == _N
        # the new schema HAS degree_min; carried rows default it to NULL
        assert "degree_min" in {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
        assert con.execute("SELECT degree_min FROM jobs LIMIT 1").fetchone()[0] is None
    finally:
        con.close()


def test_carry_forward_excludes_crawled(tmp_path):
    prev = tmp_path / "prev.sqlite"
    _build_prev(prev)

    out = tmp_path / "out.sqlite"
    fresh_db(out)
    con = connect(out)
    try:
        con.execute("INSERT INTO companies(company_key, display_name) VALUES('acme', 'Acme')")
        con.commit()
        # acme was re-crawled this run -> its prior rows must NOT carry
        carried = carry_forward(con, prev, crawled_keys={"acme"})
        assert carried == 0
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        con.close()


def test_build_delta_older_schema_prev(tmp_path):
    """build_delta's change test must tolerate a prev that lacks a schema-v2 column (was the
    second freeze: "no such column: p.degree_min" crashing the post-publish delta step)."""
    prev = tmp_path / "prev.sqlite"  # older schema, N jobs
    _build_prev(prev)

    curr = tmp_path / "curr.sqlite"  # v2 schema: the same N carried rows + 5 genuinely new
    fresh_db(curr)
    con = connect(curr)
    try:
        con.execute("INSERT INTO companies(company_key, display_name) VALUES('acme', 'Acme')")
        con.executemany(
            "INSERT INTO jobs(id, content_hash, company_key, source, company, title, remote, "
            "level, employment_type, first_seen, last_seen, fetched_at, build_id) "
            "VALUES(?, ?, 'acme', 'greenhouse', 'Acme', ?, 'unknown', 'mid', 'fulltime', "
            "'2026-07-01', '2026-07-01', '2026-07-01', 'b1')",
            [(f"job-{i}", f"h{i}", f"Engineer {i}") for i in range(_N + 5)],
        )
        con.commit()
    finally:
        con.close()

    out = tmp_path / "delta.sqlite"
    info = build_delta(prev, curr, out, from_build_id="b0", to_build_id="b1")
    # No crash, and the N carried rows match on shared columns (not falsely re-sent); only the 5
    # new ids are upserts. (build_id differs but is a _DELTA_VOLATILE_COL, so it's excluded.)
    assert info["upserts"] == 5
    assert info["deletes"] == 0
