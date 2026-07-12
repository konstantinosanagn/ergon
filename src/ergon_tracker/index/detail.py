"""Tier-3 detail sidecar: recovered structured fields + snippet from per-posting JD detail fetches,
keyed by posting id with a sig for re-crawl-safe carry-forward. The JD text itself is never stored."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

DETAIL_SCHEMA_VERSION = 1
DETAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_detail (
  id TEXT PRIMARY KEY,
  sig TEXT,
  fetched_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  snippet TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
  years_min INTEGER, years_max INTEGER,
  degree_min TEXT, degree_required INTEGER,
  sponsorship_offered INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ensure_detail_schema(con: sqlite3.Connection) -> None:
    con.executescript(DETAIL_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(DETAIL_SCHEMA_VERSION),))
    con.commit()


def open_detail(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    ensure_detail_schema(con)
    return con


def detail_sig(row: dict[str, Any]) -> str:
    """Change signal for a posting, INDEPENDENT of the (to-be-fetched) description — so we only
    re-fetch when the posting materially changed. Uses content_hash if present, else title+level."""
    basis = row.get("content_hash") or f"{row.get('title', '')}|{row.get('level', '')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DetailRef:
    id: str
    source: str
    token: str | None
    apply_url: str | None
    listing_url: str | None
    content_sig: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DetailRef:
        return cls(
            id=str(row["id"]),
            source=str(row.get("source") or ""),
            token=row.get("board_token"),
            apply_url=row.get("apply_url"),
            listing_url=row.get("listing_url"),
            content_sig=detail_sig(row),
        )
