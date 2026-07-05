import gzip
import hashlib
import json

from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.index.cache import IndexCache
from ergon_tracker.index.db import SCHEMA_VERSION
from ergon_tracker.models import JobPosting


def _publish(remote_dir, tmp_path):
    src = tmp_path / "src.sqlite"
    build_index(
        [JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Eng")],
        src,
        build_id="b1",
    )
    raw = src.read_bytes()
    (remote_dir / "index.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote_dir / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b1",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )


def test_cache_downloads_verifies_and_opens(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path)
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    path = cache.ensure_fresh()
    assert path is not None and path.exists()
    assert SqliteIndexBackend(path).available() is True


def test_cache_rejects_corrupt_download(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path)
    (remote / "manifest.json").write_text(
        json.dumps(
            {"build_id": "b1", "sha256": "0" * 64, "bytes": 1, "schema_version": SCHEMA_VERSION}
        )
    )
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None


def test_cache_rejects_future_schema_version(tmp_path):
    # Forward-compat: when a future build bumps SCHEMA_VERSION, an older client must fall back
    # to live (None), never crash on an index it can't read.
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path)
    man = json.loads((remote / "manifest.json").read_text())
    man["schema_version"] = 999  # newer than this client understands
    (remote / "manifest.json").write_text(json.dumps(man))
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # graceful live fallback, no exception


def test_shardcache_rejects_future_schema_version(tmp_path):
    import gzip as _gz

    from ergon_tracker.index.build import build_sharded_index
    from ergon_tracker.index.cache import ShardCache
    from ergon_tracker.models import SearchQuery

    src = tmp_path / "build"
    build_sharded_index(
        [
            JobPosting.create(
                source="greenhouse",
                source_job_id="1",
                company="Stripe",
                title="Eng",
                sector="Fintech",
            )
        ],
        src,
        build_id="b1",
    )
    remote = tmp_path / "remote"
    remote.mkdir()
    man = json.loads((src / "shards.json").read_text())
    man["schema_version"] = 999
    (remote / "shards.json").write_text(json.dumps(man))
    for f in src.glob("shard-*.sqlite"):
        (remote / (f.name + ".gz")).write_bytes(_gz.compress(f.read_bytes()))
    cache = ShardCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure(SearchQuery(sector="Fintech")) is None  # graceful fallback


def _job(sid, company, title, **kw):
    from ergon_tracker.models import Location, RemoteType

    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=company,
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def test_cache_applies_delta_instead_of_full_download(tmp_path):
    # Returning user one build behind gets the new state via a small delta, NOT a full re-download.
    from ergon_tracker.index.build import build_delta, build_index

    remote = tmp_path / "remote"
    remote.mkdir()
    cache_dir = tmp_path / "cache"

    # Build b1, publish full, prime the local cache to b1.
    b1 = tmp_path / "b1.sqlite"
    build_index([_job("1", "Stripe", "Backend Engineer")], b1, build_id="b1")
    raw1 = b1.read_bytes()
    (remote / "index.sqlite.gz").write_bytes(gzip.compress(raw1))
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b1",
                "sha256": hashlib.sha256(raw1).hexdigest(),
                "bytes": len(raw1),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=cache_dir)
    assert cache.ensure_fresh() is not None  # now cached at b1

    # Build b2 (adds a job), publish its manifest + a b1->b2 delta, but make the FULL file
    # un-downloadable so success can ONLY come from the delta path.
    b2 = tmp_path / "b2.sqlite"
    build_index(
        [_job("1", "Stripe", "Backend Engineer"), _job("2", "Ramp", "Founding Engineer")],
        b2,
        build_id="b2",
    )
    raw2 = b2.read_bytes()
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b2",
                "sha256": hashlib.sha256(raw2).hexdigest(),
                "bytes": len(raw2),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    (remote / "index.sqlite.gz").write_bytes(b"corrupt-not-gzip")  # full path must NOT be used
    delta = tmp_path / "delta.sqlite"
    info = build_delta(b1, b2, delta, from_build_id="b1", to_build_id="b2")
    assert info["upserts"] == 1
    draw = delta.read_bytes()
    (remote / "index-delta.sqlite.gz").write_bytes(gzip.compress(draw))
    (remote / "manifest-delta.json").write_text(
        json.dumps(
            {
                "from_build_id": "b1",
                "to_build_id": "b2",
                "sha256": hashlib.sha256(draw).hexdigest(),
                "bytes": len(draw),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )

    path = cache.ensure_fresh()  # must apply the delta (full file is corrupt)
    assert path is not None
    backend = SqliteIndexBackend(path)
    from ergon_tracker.models import SearchQuery

    titles = {j.title for j in backend.search(SearchQuery(keywords="engineer", limit=10))}
    assert "Founding Engineer" in titles  # the b2 row arrived via the delta
    assert json.loads((cache_dir / "manifest.json").read_text())["build_id"] == "b2"


def test_cache_falls_back_to_full_when_delta_base_mismatches(tmp_path):
    # Local is at b1 but the only delta bridges b0->b2: must ignore it and full-download b2.
    from ergon_tracker.index.build import build_delta, build_index

    remote = tmp_path / "remote"
    remote.mkdir()
    cache_dir = tmp_path / "cache"
    b1 = tmp_path / "b1.sqlite"
    build_index([_job("1", "Stripe", "Backend Engineer")], b1, build_id="b1")
    raw1 = b1.read_bytes()
    (remote / "index.sqlite.gz").write_bytes(gzip.compress(raw1))
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b1",
                "sha256": hashlib.sha256(raw1).hexdigest(),
                "bytes": len(raw1),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=cache_dir)
    assert cache.ensure_fresh() is not None  # cached at b1

    b2 = tmp_path / "b2.sqlite"
    build_index(
        [_job("1", "Stripe", "Backend Engineer"), _job("2", "Ramp", "Founding Engineer")],
        b2,
        build_id="b2",
    )
    raw2 = b2.read_bytes()
    (remote / "index.sqlite.gz").write_bytes(gzip.compress(raw2))  # full IS valid here
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b2",
                "sha256": hashlib.sha256(raw2).hexdigest(),
                "bytes": len(raw2),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    # a delta that does NOT bridge b1 (claims b0->b2)
    b0 = tmp_path / "b0.sqlite"
    build_index([_job("9", "Zzz", "Old Role")], b0, build_id="b0")
    delta = tmp_path / "delta.sqlite"
    build_delta(b0, b2, delta, from_build_id="b0", to_build_id="b2")
    draw = delta.read_bytes()
    (remote / "index-delta.sqlite.gz").write_bytes(gzip.compress(draw))
    (remote / "manifest-delta.json").write_text(
        json.dumps(
            {
                "from_build_id": "b0",
                "to_build_id": "b2",
                "sha256": hashlib.sha256(draw).hexdigest(),
                "bytes": len(draw),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    path = cache.ensure_fresh()  # delta base mismatch -> full download of b2
    assert path is not None
    from ergon_tracker.models import SearchQuery

    titles = {
        j.title for j in SqliteIndexBackend(path).search(SearchQuery(keywords="engineer", limit=10))
    }
    assert "Founding Engineer" in titles and "Old Role" not in titles  # got b2, not the b0 delta


def test_cache_applies_delta_chain_when_multiple_builds_behind(tmp_path):
    # A user 2 builds behind (b0) catches up to b2 by chaining deltas via deltas.json — no full
    # download. The full file is corrupt so success can ONLY come from the chain.
    from ergon_tracker.index.build import build_delta, build_index

    remote = tmp_path / "remote"
    remote.mkdir()
    cache_dir = tmp_path / "cache"

    def _job2(sid, company, title):
        from ergon_tracker.models import Location, RemoteType

        return JobPosting.create(
            source="greenhouse",
            source_job_id=sid,
            company=company,
            title=title,
            locations=[Location(raw="Remote", is_remote=True)],
            remote=RemoteType.REMOTE,
        )

    b0 = tmp_path / "b0.sqlite"
    b1 = tmp_path / "b1.sqlite"
    b2 = tmp_path / "b2.sqlite"
    build_index([_job2("1", "Stripe", "Backend Engineer")], b0, build_id="b0")
    build_index(
        [_job2("1", "Stripe", "Backend Engineer"), _job2("2", "Ramp", "Frontend Eng")],
        b1,
        build_id="b1",
    )
    build_index(
        [
            _job2("1", "Stripe", "Backend Engineer"),
            _job2("2", "Ramp", "Frontend Eng"),
            _job2("3", "Cursor", "Founding Engineer"),
        ],
        b2,
        build_id="b2",
    )

    # prime local cache at b0
    cache_dir.mkdir()
    (cache_dir / "index.sqlite").write_bytes(b0.read_bytes())
    (cache_dir / "manifest.json").write_text(
        json.dumps({"build_id": "b0", "schema_version": SCHEMA_VERSION})
    )

    # remote: b2 full (corrupt so only the chain works) + manifest + deltas window + chain files
    raw2 = b2.read_bytes()
    (remote / "index.sqlite.gz").write_bytes(b"corrupt-full")
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b2",
                "sha256": hashlib.sha256(raw2).hexdigest(),
                "bytes": len(raw2),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    # the 1-behind manifest-delta only bridges b1->b2 (won't match local b0) -> chain kicks in
    d01, d12 = tmp_path / "d01.sqlite", tmp_path / "d12.sqlite"
    build_delta(b0, b1, d01, from_build_id="b0", to_build_id="b1")
    build_delta(b1, b2, d12, from_build_id="b1", to_build_id="b2")
    g01, g12 = gzip.compress(d01.read_bytes()), gzip.compress(d12.read_bytes())
    (remote / "index-delta-b1.sqlite.gz").write_bytes(g01)
    (remote / "index-delta-b2.sqlite.gz").write_bytes(g12)
    (remote / "manifest-delta.json").write_text(
        json.dumps(
            {
                "from_build_id": "b1",
                "to_build_id": "b2",
                "sha256": hashlib.sha256(d12.read_bytes()).hexdigest(),
                "bytes": len(d12.read_bytes()),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    (remote / "deltas.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "deltas": [
                    {
                        "from_build_id": "b0",
                        "to_build_id": "b1",
                        "file": "index-delta-b1.sqlite.gz",
                        "sha256": hashlib.sha256(d01.read_bytes()).hexdigest(),
                        "bytes": len(d01.read_bytes()),
                    },
                    {
                        "from_build_id": "b1",
                        "to_build_id": "b2",
                        "file": "index-delta-b2.sqlite.gz",
                        "sha256": hashlib.sha256(d12.read_bytes()).hexdigest(),
                        "bytes": len(d12.read_bytes()),
                    },
                ],
            }
        )
    )

    cache = IndexCache(base_url=remote.as_uri(), cache_dir=cache_dir)
    path = cache.ensure_fresh()
    assert path is not None
    from ergon_tracker.models import SearchQuery

    titles = {
        j.title for j in SqliteIndexBackend(path).search(SearchQuery(keywords="engineer", limit=10))
    }
    assert "Founding Engineer" in titles  # reached b2 via the 2-delta chain
    assert json.loads((cache_dir / "manifest.json").read_text())["build_id"] == "b2"


def test_delta_chain_failure_leaves_local_db_unchanged(tmp_path):
    # If a chain step fails (corrupt delta), the cached db must stay at its original build (not be
    # half-advanced), so the full-download fallback starts from a clean base.
    from ergon_tracker.index.build import build_delta, build_index
    from ergon_tracker.index.db import connect

    def _job2(sid, company, title):
        from ergon_tracker.models import Location, RemoteType

        return JobPosting.create(
            source="greenhouse",
            source_job_id=sid,
            company=company,
            title=title,
            locations=[Location(raw="Remote", is_remote=True)],
            remote=RemoteType.REMOTE,
        )

    remote = tmp_path / "remote"
    remote.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    b0 = tmp_path / "b0.sqlite"
    b1 = tmp_path / "b1.sqlite"
    b2 = tmp_path / "b2.sqlite"
    build_index([_job2("1", "Stripe", "Backend Engineer")], b0, build_id="b0")
    build_index(
        [_job2("1", "Stripe", "Backend Engineer"), _job2("2", "Ramp", "Frontend Eng")],
        b1,
        build_id="b1",
    )
    build_index(
        [
            _job2("1", "Stripe", "Backend Engineer"),
            _job2("2", "Ramp", "Frontend Eng"),
            _job2("3", "Cursor", "Founding Engineer"),
        ],
        b2,
        build_id="b2",
    )
    (cache_dir / "index.sqlite").write_bytes(b0.read_bytes())
    (cache_dir / "manifest.json").write_text(
        json.dumps({"build_id": "b0", "schema_version": SCHEMA_VERSION})
    )

    raw2 = b2.read_bytes()
    # full download ALSO unavailable so we isolate the chain behavior (ensure_fresh returns None)
    (remote / "index.sqlite.gz").write_bytes(b"corrupt-full")
    (remote / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "b2",
                "sha256": hashlib.sha256(raw2).hexdigest(),
                "bytes": len(raw2),
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    (remote / "manifest-delta.json").write_text(
        json.dumps(
            {
                "from_build_id": "b1",
                "to_build_id": "b2",
                "sha256": "x",
                "bytes": 1,
                "schema_version": SCHEMA_VERSION,
            }
        )
    )
    d01, d12 = tmp_path / "d01.sqlite", tmp_path / "d12.sqlite"
    build_delta(b0, b1, d01, from_build_id="b0", to_build_id="b1")
    build_delta(b1, b2, d12, from_build_id="b1", to_build_id="b2")
    (remote / "index-delta-b1.sqlite.gz").write_bytes(gzip.compress(d01.read_bytes()))
    (remote / "index-delta-b2.sqlite.gz").write_bytes(b"corrupt-second-delta")  # mid-chain failure
    (remote / "deltas.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "deltas": [
                    {
                        "from_build_id": "b0",
                        "to_build_id": "b1",
                        "file": "index-delta-b1.sqlite.gz",
                        "sha256": hashlib.sha256(d01.read_bytes()).hexdigest(),
                        "bytes": len(d01.read_bytes()),
                    },
                    {
                        "from_build_id": "b1",
                        "to_build_id": "b2",
                        "file": "index-delta-b2.sqlite.gz",
                        "sha256": hashlib.sha256(d12.read_bytes()).hexdigest(),
                        "bytes": len(d12.read_bytes()),
                    },
                ],
            }
        )
    )

    cache = IndexCache(base_url=remote.as_uri(), cache_dir=cache_dir)
    cache.ensure_fresh()  # chain fails on the corrupt 2nd delta; full also corrupt -> no update
    # the cached db must still be the ORIGINAL b0 (not half-advanced to b1)
    con = connect(cache_dir / "index.sqlite", read_only=True)
    bid = con.execute("SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
    titles = {r[0] for r in con.execute("SELECT title FROM jobs")}
    con.close()
    assert bid == "b0"
    assert titles == {"Backend Engineer"}  # untouched; Frontend Eng (b1) did NOT leak in


def test_cached_index_build_id_reads_manifest(tmp_path):
    from ergon_tracker.index.cache import cached_index_build_id

    assert cached_index_build_id(tmp_path) is None  # nothing cached yet
    (tmp_path / "manifest.json").write_text(json.dumps({"build_id": "build-2026-06-19-7"}))
    assert cached_index_build_id(tmp_path) == "build-2026-06-19-7"
    # falls back to the slim manifest when the full one is absent
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "manifest-slim.json").write_text(json.dumps({"build_id": "slim-build-9"}))
    assert cached_index_build_id(d2) == "slim-build-9"
