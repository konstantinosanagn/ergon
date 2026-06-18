"""IndexBackend protocol + the SQLite implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import JobPosting, Provenance, SearchQuery
from .db import SCHEMA_VERSION, connect
from .mapping import from_row
from .query import search_rows


@runtime_checkable
class IndexBackend(Protocol):
    def available(self) -> bool: ...
    def metadata(self) -> dict: ...
    def search(self, query: SearchQuery) -> list[JobPosting]: ...


class SqliteIndexBackend:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def available(self) -> bool:
        if not self.path.exists():
            return False
        try:
            con = connect(self.path, read_only=True)
            v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            con.close()
            return bool(v) and int(v[0]) == SCHEMA_VERSION
        except Exception:  # noqa: BLE001 - any open/read failure => not usable
            return False

    def metadata(self) -> dict:
        con = connect(self.path, read_only=True)
        try:
            meta = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM meta")}
            return {
                "schema_version": int(meta.get("schema_version", 0)),
                "build_id": meta.get("build_id"),
                "row_count": int(meta.get("row_count", 0)),
            }
        finally:
            con.close()

    def search(self, query: SearchQuery) -> list[JobPosting]:
        con = connect(self.path, read_only=True)
        try:
            jobs: list[JobPosting] = []
            for row in search_rows(con, query):
                job = from_row(row)
                src = con.execute(
                    "SELECT source,source_job_id,apply_url,fetched_at FROM job_sources "
                    "WHERE job_id=?",
                    (job.id,),
                ).fetchall()
                if src:
                    job.provenance = [
                        Provenance(
                            source=s["source"],
                            source_job_id=s["source_job_id"],
                            apply_url=s["apply_url"],
                        )
                        for s in src
                    ]
                jobs.append(job)
            return jobs
        finally:
            con.close()
