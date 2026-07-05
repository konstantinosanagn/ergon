"""Full tiered-index pipeline end-to-end (CI-safe, synthetic): full -> slim -> delta through the
real IndexCache/SlimCache, simulating fresh + returning anonymous users with zero network/ATS.

A regression guard for the v1+v2 contract: the stress test we run by hand against the live 380K-job
index, shrunk to a deterministic fixture so it runs in CI.
"""

from __future__ import annotations

import gzip
import hashlib
import json

from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_delta, build_index, build_slim_index
from ergon_tracker.index.cache import IndexCache, SlimCache
from ergon_tracker.index.db import SCHEMA_VERSION, connect
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType, SearchQuery


def _job(sid, company, title, **kw):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=company,
        title=title,
        description_text=kw.pop("desc", "a job description here for fts"),
        locations=[Location(raw=kw.pop("loc", "Remote"), is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def _publish(remote, path, gz_name, manifest_name, extra):
    raw = path.read_bytes()
    (remote / gz_name).write_bytes(gzip.compress(raw))
    (remote / manifest_name).write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "schema_version": SCHEMA_VERSION,
                **extra,
            }
        )
    )


def _ids(path):
    con = connect(path, read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT id FROM jobs")}
    finally:
        con.close()


def test_full_tiered_pipeline_fresh_and_returning_user(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()

    # day0 and day1 full indexes with churn between them: 1 changed, 1 deleted, 1 added.
    base = [
        _job(
            "1",
            "Stripe",
            "Senior Backend Engineer",
            level=JobLevel.SENIOR,
            sector="Fintech",
            city="New York",
        ),
        _job(
            "2", "Ramp", "Frontend Engineer", level=JobLevel.MID, sector="Fintech", city="New York"
        ),
        _job(
            "3",
            "OpenAI",
            "ML Engineer",
            level=JobLevel.SENIOR,
            sector="AI/ML",
            city="San Francisco",
        ),
        _job(
            "4",
            "Anthropic",
            "Research Engineer",
            level=JobLevel.SENIOR,
            sector="AI/ML",
            city="San Francisco",
        ),
    ]
    day1 = [
        base[0],
        _job(
            "2",
            "Ramp",
            "Staff Frontend Engineer",
            level=JobLevel.STAFF,
            sector="Fintech",
            city="New York",
        ),  # changed
        # job 3 (OpenAI) deleted
        base[3],
        _job(
            "5",
            "Cursor",
            "Founding Engineer",
            level=JobLevel.SENIOR,
            sector="AI/ML",
            city="San Francisco",
        ),  # new
    ]
    prev = tmp_path / "prev.sqlite"
    curr = tmp_path / "curr.sqlite"
    build_index(base, prev, build_id="day0")
    build_index(day1, curr, build_id="day1")

    _publish(remote, curr, "index.sqlite.gz", "manifest.json", {"build_id": "day1"})
    slim = tmp_path / "slim.sqlite"
    build_slim_index(curr, slim, build_id="day1")
    _publish(remote, slim, "index-slim.sqlite.gz", "manifest-slim.json", {"build_id": "day1"})
    delta = tmp_path / "delta.sqlite"
    build_delta(prev, curr, delta, from_build_id="day0", to_build_id="day1")
    _publish(
        remote,
        delta,
        "index-delta.sqlite.gz",
        "manifest-delta.json",
        {"from_build_id": "day0", "to_build_id": "day1"},
    )

    # --- Fresh user: cold cache -> full download -> queryable ---
    fresh = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "fresh")
    fpath = fresh.ensure_fresh()
    assert fpath is not None and SqliteIndexBackend(fpath).available()
    assert len(SqliteIndexBackend(fpath).search(SearchQuery(keywords="engineer", limit=10))) > 0

    # --- Returning user one build behind -> delta apply -> equals the full day1 rebuild ---
    retdir = tmp_path / "ret"
    retdir.mkdir()
    (retdir / "index.sqlite").write_bytes(prev.read_bytes())
    (retdir / "manifest.json").write_text(
        json.dumps({"build_id": "day0", "schema_version": SCHEMA_VERSION})
    )
    # make the FULL file un-downloadable so success can ONLY come from the delta path
    (remote / "index.sqlite.gz").write_bytes(b"corrupt")
    ret = IndexCache(base_url=remote.as_uri(), cache_dir=retdir)
    rpath = ret.ensure_fresh()
    assert rpath is not None
    assert _ids(rpath) == _ids(curr)  # delta-applied == full rebuild
    con = connect(rpath, read_only=True)
    try:
        assert con.execute("SELECT value FROM meta WHERE key='build_id'").fetchone()[0] == "day1"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        titles = {r[0] for r in con.execute("SELECT title FROM jobs")}
        assert "Founding Engineer" in titles  # new row arrived
        assert "Staff Frontend Engineer" in titles and "Frontend Engineer" not in titles  # changed
        assert "ML Engineer" not in titles  # deleted
    finally:
        con.close()
    # restore the real full file for the slim scenario
    _publish(remote, curr, "index.sqlite.gz", "manifest.json", {"build_id": "day1"})

    # --- Slim tier: broad structured-filter query returns IDENTICAL ids to the full index ---
    slimc = SlimCache(base_url=remote.as_uri(), cache_dir=tmp_path / "slim_cache")
    spath = slimc.ensure_fresh()
    assert spath is not None
    sb, fb = SqliteIndexBackend(spath), SqliteIndexBackend(curr)
    for q in [
        SearchQuery(level=JobLevel.SENIOR, limit=50),
        SearchQuery(sector="AI/ML", city="San Francisco", limit=50),
        SearchQuery(city="New York", limit=50),
    ]:
        assert [j.id for j in sb.search(q)] == [j.id for j in fb.search(q)]
