"""Build -> publish to a temp 'release' -> cache downloads+verifies -> query -> live fallback."""

import gzip
import hashlib
import json

import ergon_tracker.index.router as router
from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.index.cache import IndexCache
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType, SearchQuery


def _jobs():
    return [
        JobPosting.create(
            source="greenhouse",
            source_job_id="1",
            company="Stripe",
            title="Senior Backend Engineer",
            level=JobLevel.SENIOR,
            sector="Fintech",
            locations=[Location(raw="Remote", is_remote=True)],
            remote=RemoteType.REMOTE,
        ),
        JobPosting.create(
            source="lever",
            source_job_id="2",
            company="Ramp",
            title="Frontend Engineer",
            locations=[Location(raw="Remote", is_remote=True)],
            remote=RemoteType.REMOTE,
        ),
    ]


def test_full_pipeline_offline(tmp_path, monkeypatch):
    src = tmp_path / "src.sqlite"
    build_index(_jobs(), src, build_id="b1")
    remote = tmp_path / "remote"
    remote.mkdir()
    raw = src.read_bytes()
    (remote / "index.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b1",
                "schema_version": 1,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
    )
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    # Hermetic: force the single-file path (no live shard fetch over the network).
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(
        router,
        "_load_backend",
        lambda: (lambda p: SqliteIndexBackend(p) if p else None)(cache.ensure_fresh()),
    )

    res = router.try_index(SearchQuery(keywords="backend", limit=10))
    assert res is not None and len(res) == 1 and res[0].company == "Stripe"

    # remove the index everywhere -> graceful fallback signal (None), no raise
    (cache.cache_dir / "index.sqlite").unlink()
    (remote / "index.sqlite.gz").unlink()
    (remote / "manifest.json").unlink()
    assert router.try_index(SearchQuery(keywords="backend", limit=10)) is None
