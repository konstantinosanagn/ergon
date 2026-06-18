import sqlite3

import pytest

from ergon_tracker.index.db import SCHEMA_VERSION, connect, fresh_db


def test_fresh_db_has_expected_tables(tmp_path):
    p = tmp_path / "i.sqlite"
    fresh_db(p)
    con = connect(p)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"companies", "jobs", "job_sources", "job_events", "job_tags", "meta"} <= names
    fts = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE name='jobs_fts'")}
    assert "jobs_fts" in fts


def test_check_constraint_rejects_bad_remote(tmp_path):
    p = tmp_path / "i.sqlite"
    fresh_db(p)
    con = connect(p)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO jobs(id,content_hash,source,company,title,remote,level,"
            "employment_type,status,first_seen,last_seen,fetched_at,build_id) "
            "VALUES('a','h','greenhouse','Co','T','BOGUS','mid','full_time',"
            "'active','d','d','d','b')"
        )


def test_schema_version_is_int():
    assert isinstance(SCHEMA_VERSION, int) and SCHEMA_VERSION >= 1
