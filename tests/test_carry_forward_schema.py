"""Schema-v2 carry-forward regression: a prev index built on an OLDER schema (missing the
degree_min/degree_required columns added in schema v2) must still carry forward — the daily
index froze 2026-07-05 when the fixed-column SELECT hit "no such column", dropped the whole
backlog, and the row_floor gate then kept the stale snapshot (a self-deadlock).
"""

from __future__ import annotations

import re

from ergon_tracker.index.build import carry_forward
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
