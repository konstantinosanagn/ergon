"""IndexBackend protocol + the SQLite implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import JobPosting, Provenance, SearchQuery
from .build import sector_slug
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


class ShardedIndexBackend:
    """IndexBackend over per-sector shards: a sector query opens one shard; cross-sector fans out."""

    def __init__(self, shard_dir: Path | str) -> None:
        self.dir = Path(shard_dir)
        self.manifest_path = self.dir / "shards.json"

    def _manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def available(self) -> bool:
        m = self._manifest()
        return bool(m.get("shards")) and int(m.get("schema_version", 0)) == SCHEMA_VERSION

    def metadata(self) -> dict:
        m = self._manifest()
        shards = m.get("shards", {})
        return {
            "schema_version": int(m.get("schema_version", 0)),
            "build_id": m.get("build_id"),
            "row_count": sum(int(s.get("rows", 0)) for s in shards.values()),
            "shards": len(shards),
        }

    def search(self, query: SearchQuery) -> list[JobPosting]:
        shards = self._manifest().get("shards", {})
        if query.sector:
            slug = sector_slug(query.sector)
            targets = [shards[slug]["file"]] if slug in shards else []
        else:
            targets = [s["file"] for s in shards.values()]

        results: list[JobPosting] = []
        for fname in targets:
            be = SqliteIndexBackend(self.dir / fname)
            if be.available():
                results.extend(be.search(query))

        # Cross-shard merge: bm25 scores aren't comparable across shards, so re-rank the union.
        if query.keywords:
            from ..ranking import rank

            results = rank(results, query.keywords)
        else:
            results.sort(key=lambda j: (j.posted_at is not None, j.posted_at), reverse=True)
        limit = query.limit or len(results)
        return results[:limit]
