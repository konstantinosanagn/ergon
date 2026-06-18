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
