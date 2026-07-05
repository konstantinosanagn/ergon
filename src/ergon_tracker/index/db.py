"""SQLite connection + fresh-DB helpers for the search index."""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

# v2: degree_min/degree_required columns (+ idx_jobs_degree) for the education filter.
SCHEMA_VERSION = 2


def _schema_sql() -> str:
    return (files("ergon_tracker.index") / "schema.sql").read_text(encoding="utf-8")


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA query_only = ON")
    else:
        con = sqlite3.connect(str(path))
        con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


def fresh_db(path: Path | str) -> None:
    """Create a new index DB with the full schema (overwrites any existing file)."""
    p = Path(path)
    p.unlink(missing_ok=True)
    con = connect(p)
    try:
        con.executescript(_schema_sql())
        con.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
        )
        con.commit()
    finally:
        con.close()
