import json

from ergon_tracker.index.build import build_sharded_index, sector_slug
from ergon_tracker.index.db import connect
from ergon_tracker.models import JobPosting, Location, RemoteType


def _job(sid, company, title, sector=None):
    return JobPosting.create(
        source="greenhouse", source_job_id=sid, company=company, title=title, sector=sector,
        locations=[Location(raw="Remote", is_remote=True)], remote=RemoteType.REMOTE,
    )


def test_sector_slug():
    assert sector_slug("AI/ML") == "ai-ml"
    assert sector_slug("Fintech") == "fintech"
    assert sector_slug(None) == "unknown"
    assert sector_slug("") == "unknown"


def test_build_sharded_writes_one_shard_per_sector(tmp_path):
    jobs = [
        _job("1", "Stripe", "Backend Engineer", sector="Fintech"),
        _job("2", "Ramp", "Frontend Engineer", sector="Fintech"),
        _job("3", "Hugging Face", "ML Engineer", sector="AI/ML"),
        _job("4", "Acme", "Generalist", sector=None),
    ]
    manifest = build_sharded_index(jobs, tmp_path, build_id="b1")
    shards = manifest["shards"]
    assert set(shards) == {"fintech", "ai-ml", "unknown"}
    assert shards["fintech"]["rows"] == 2
    assert shards["ai-ml"]["rows"] == 1 and shards["unknown"]["rows"] == 1
    # rows sum to total
    assert sum(s["rows"] for s in shards.values()) == 4
    # manifest persisted + each shard file is a valid queryable index
    assert json.loads((tmp_path / "shards.json").read_text())["shards"].keys() == shards.keys()
    con = connect(tmp_path / "shard-fintech.sqlite", read_only=True)
    titles = {r[0] for r in con.execute("SELECT title FROM jobs")}
    assert titles == {"Backend Engineer", "Frontend Engineer"}
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert all("sha256" in s for s in shards.values())


def test_sharded_backend_sector_and_cross_sector(tmp_path):
    from ergon_tracker.index.backend import ShardedIndexBackend
    from ergon_tracker.models import SearchQuery

    jobs = [
        _job("1", "Stripe", "Backend Engineer", sector="Fintech"),
        _job("2", "Ramp", "Payments Engineer", sector="Fintech"),
        _job("3", "Hugging Face", "ML Engineer", sector="AI/ML"),
    ]
    build_sharded_index(jobs, tmp_path, build_id="b1")
    be = ShardedIndexBackend(tmp_path)
    assert be.available() and be.metadata()["row_count"] == 3 and be.metadata()["shards"] == 2

    # sector-scoped query -> only that shard's jobs
    fin = be.search(SearchQuery(keywords="engineer", sector="Fintech", limit=10))
    assert {j.company for j in fin} == {"Stripe", "Ramp"}

    # cross-sector query (no sector) -> merged across shards, re-ranked
    allq = be.search(SearchQuery(keywords="engineer", limit=10))
    assert {j.company for j in allq} == {"Stripe", "Ramp", "Hugging Face"}


def test_sharded_parity_with_single_file(tmp_path):
    from ergon_tracker.index.backend import ShardedIndexBackend, SqliteIndexBackend
    from ergon_tracker.index.build import build_index
    from ergon_tracker.models import SearchQuery

    jobs = [
        _job("1", "Stripe", "Backend Engineer", sector="Fintech"),
        _job("2", "Hugging Face", "ML Engineer", sector="AI/ML"),
        _job("3", "Acme", "Data Engineer", sector=None),
    ]
    single = tmp_path / "single.sqlite"
    build_index(jobs, single, build_id="b1")
    sharded_dir = tmp_path / "shards"
    build_sharded_index(jobs, sharded_dir, build_id="b1")

    q = SearchQuery(keywords="engineer", limit=10)
    single_ids = {j.id for j in SqliteIndexBackend(single).search(q)}
    sharded_ids = {j.id for j in ShardedIndexBackend(sharded_dir).search(q)}
    assert single_ids == sharded_ids  # same result set, just stored sharded


def _publish_shards(remote, src_dir):
    """gzip each shard + copy shards.json into a file:// 'remote' (mimics the release assets)."""
    import gzip
    import shutil
    remote.mkdir(parents=True, exist_ok=True)
    shutil.copy(src_dir / "shards.json", remote / "shards.json")
    for f in src_dir.glob("shard-*.sqlite"):
        (remote / (f.name + ".gz")).write_bytes(gzip.compress(f.read_bytes()))


def test_shardcache_downloads_only_needed_shard(tmp_path):
    from ergon_tracker.index.cache import ShardCache
    from ergon_tracker.models import SearchQuery

    src = tmp_path / "build"
    build_sharded_index(
        [_job("1", "Stripe", "Backend Engineer", sector="Fintech"),
         _job("2", "HF", "ML Engineer", sector="AI/ML")],
        src, build_id="b1",
    )
    remote = tmp_path / "remote"
    _publish_shards(remote, src)
    cache = ShardCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")

    d = cache.ensure(SearchQuery(keywords="engineer", sector="Fintech", limit=5))
    assert d is not None
    files = {f.name for f in d.glob("shard-*.sqlite")}
    assert files == {"shard-fintech.sqlite"}  # ONLY the fintech shard pulled

    # cross-sector pulls all shards
    d2 = cache.ensure(SearchQuery(keywords="engineer", limit=5))
    files2 = {f.name for f in d2.glob("shard-*.sqlite")}
    assert files2 == {"shard-fintech.sqlite", "shard-ai-ml.sqlite"}


def test_shardcache_missing_sector_returns_none(tmp_path):
    from ergon_tracker.index.cache import ShardCache
    from ergon_tracker.models import SearchQuery

    src = tmp_path / "build"
    build_sharded_index([_job("1", "Stripe", "Eng", sector="Fintech")], src, build_id="b1")
    remote = tmp_path / "remote"
    _publish_shards(remote, src)
    cache = ShardCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure(SearchQuery(sector="Healthcare")) is None  # no such shard -> fallback


def test_sharded_from_db_parity_with_in_memory(tmp_path):
    # build_sharded_index_from_db (SQL, memory-bounded) must produce the same shards as the
    # in-memory build_sharded_index for the same jobs.
    from ergon_tracker.index.build import (
        build_index,
        build_sharded_index,
        build_sharded_index_from_db,
    )
    from ergon_tracker.index.db import connect

    jobs = [
        _job("1", "Stripe", "Backend Engineer", sector="Fintech"),
        _job("2", "Ramp", "Payments Engineer", sector="Fintech"),
        _job("3", "Hugging Face", "ML Engineer", sector="AI/ML"),
        _job("4", "Acme", "Generalist", sector=None),
    ]
    mem = build_sharded_index(jobs, tmp_path / "mem", build_id="b1")

    src = tmp_path / "index.sqlite"
    build_index(jobs, src, build_id="b1")
    db = build_sharded_index_from_db(src, tmp_path / "db", build_id="b1")

    assert set(mem["shards"]) == set(db["shards"])  # same slugs
    for slug in mem["shards"]:
        assert mem["shards"][slug]["rows"] == db["shards"][slug]["rows"]
    # ids per shard match
    for slug in db["shards"]:
        a = {r[0] for r in connect(tmp_path / "mem" / f"shard-{slug}.sqlite", read_only=True).execute("SELECT id FROM jobs")}
        b = {r[0] for r in connect(tmp_path / "db" / f"shard-{slug}.sqlite", read_only=True).execute("SELECT id FROM jobs")}
        assert a == b
    # the unknown shard exists and holds the sector-less job
    assert db["shards"]["unknown"]["rows"] == 1
