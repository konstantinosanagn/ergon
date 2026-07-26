"""Item 6 — publish core ONCE + top-level set manifest + SDK-side torn-set rejection.

Covers:
* PARITY: a build that runs the --detail merge + --liveness flips publishes the core
  ``index.sqlite.gz`` EXACTLY ONCE (was 3x: gate, post-detail, post-liveness), and that single
  published core is byte-identical to the fully-reconciled on-disk index (no intermediate re-gzip).
* The set manifest names every present asset with a matching sha, and tolerates a partition run
  that only rewrote SOME assets (join-shard case).
* The SDK-side verify (IndexCache, behind ERGON_VERIFY_SET_MANIFEST) rejects a torn set and falls
  back to the cached prior; flag-off = the pre-Item-6 download path (a torn set is ignored).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402
from tests.test_build_main_wiring import _fake_crawl_due  # noqa: E402

from ergon_tracker.index.cache import IndexCache  # noqa: E402
from ergon_tracker.index.db import SCHEMA_VERSION, connect  # noqa: E402


# --- fake reconciles: mutate the promoted core in place, create their sidecar file -----------------
async def _fake_reconcile_detail(detail_db, index_db, *, shard=None, num_shards=None, merge=True):
    detail_db.write_bytes(b"detail-sidecar-stub")  # only needs to exist for the gzip
    con = connect(index_db)
    try:
        con.execute(
            "UPDATE jobs SET snippet = 'RECOVERED_JD' WHERE id = (SELECT id FROM jobs LIMIT 1)"
        )
        con.commit()
    finally:
        con.close()
    return {"fetched": 1, "failed": 0, "missing": 0, "merged": 1}


async def _fake_reconcile_liveness(liveness_db, index_db):
    liveness_db.write_bytes(b"liveness-sidecar-stub")
    con = connect(index_db)
    try:
        con.execute(
            "UPDATE jobs SET status = 'expired' WHERE id = (SELECT id FROM jobs ORDER BY id LIMIT 1)"
        )
        con.commit()
    finally:
        con.close()
    return {
        "checked": 1,
        "flipped_dead": 1,
        "confirmed_alive": 0,
        "boards_fetched": 1,
        "boards_failed": 0,
    }


def test_core_published_once_and_captures_every_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    monkeypatch.setattr(bi, "_reconcile_detail", _fake_reconcile_detail)
    monkeypatch.setattr(bi, "_reconcile_liveness", _fake_reconcile_liveness)

    core_publishes: list[str] = []
    real_publish = bi.publish_artifacts

    def counting_publish(db_path, out_dir, *, build_id):
        core_publishes.append(Path(db_path).name)
        return real_publish(db_path, out_dir, build_id=build_id)

    monkeypatch.setattr(bi, "publish_artifacts", counting_publish)

    out = tmp_path / "dist"
    bi.main(
        ["--incremental", "--sharded", "--detail", "--liveness", "--limit-companies", "5",
         "--out", str(out)]
    )

    # THE MEASURE: the core index is gzipped EXACTLY ONCE (pre-Item-6 this was 3: gate + detail +
    # liveness). publish_artifacts is only ever called for the core, so the count is authoritative.
    assert core_publishes == ["index.sqlite"], core_publishes

    # The single published core reflects BOTH reconciles (detail merge + liveness flip) -> proof the
    # deferred single publish captured the fully-reconciled index, not a pre-reconcile snapshot.
    published_raw = gzip.decompress((out / "index.sqlite.gz").read_bytes())
    pub = tmp_path / "published.sqlite"
    pub.write_bytes(published_raw)
    con = connect(pub, read_only=True)
    snippets = {r[0] for r in con.execute("SELECT snippet FROM jobs")}
    statuses = {r[0] for r in con.execute("SELECT status FROM jobs")}
    con.close()
    assert "RECOVERED_JD" in snippets  # --detail merge landed in the published gz
    assert "expired" in statuses  # --liveness flip landed in the published gz

    # PARITY: the published gz decompresses byte-identically to the reconciled on-disk index.
    assert published_raw == (out / "index.sqlite").read_bytes()

    # The set manifest names the core with a sha that matches manifest.json (the SDK cross-check).
    set_manifest = json.loads((out / "manifest-set.json").read_text())
    core_sha = json.loads((out / "manifest.json").read_text())["sha256"]
    assert set_manifest["assets"]["index.sqlite.gz"]["sha256"] == core_sha
    assert set_manifest["build_id"].startswith("build-")


def test_set_manifest_tolerates_partial_rewrite(tmp_path):
    """A join-shard partition run rewrites only SOME assets; the set manifest must still describe
    whatever is present (core alone, then core + a carried-forward sidecar)."""
    out = tmp_path / "dist"
    out.mkdir()
    # Core-only (as a lean partition run would leave it before sidecars are added back).
    (out / "index.sqlite").write_bytes(b"fake-core")
    bi.publish_artifacts(out / "index.sqlite", out, build_id="b1")
    m1 = bi.write_set_manifest(out, build_id="b1")
    assert set(m1["assets"]) == {"index.sqlite.gz"}

    # Add a carried-forward detail sidecar (its own prior build_id) + manifest, regenerate.
    (out / "index-detail.sqlite.gz").write_bytes(b"detail")
    (out / "manifest-detail.json").write_text(
        json.dumps({"build_id": "b0", "schema_version": 1, "sha256": "d" * 64, "bytes": 6})
    )
    m2 = bi.write_set_manifest(out, build_id="b1")
    assert set(m2["assets"]) == {"index.sqlite.gz", "index-detail.sqlite.gz"}
    # The carried-forward sidecar keeps its OWN build_id + sha (partition tolerance).
    assert m2["assets"]["index-detail.sqlite.gz"] == {"sha256": "d" * 64, "bytes": 6, "build_id": "b0"}


# --- SDK-side torn-set rejection (ships dark) ------------------------------------------------------
def _publish_core(remote: Path, tmp_path: Path, build_id: str) -> tuple[bytes, str]:
    from ergon_tracker.index.build import build_index
    from ergon_tracker.models import JobPosting

    src = tmp_path / f"src-{build_id}.sqlite"
    build_index(
        [JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Eng")],
        src,
        build_id=build_id,
    )
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    (remote / "index.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest.json").write_text(
        json.dumps(
            {"build_id": build_id, "sha256": sha, "bytes": len(raw), "schema_version": SCHEMA_VERSION}
        )
    )
    return raw, sha


def _write_set_manifest(remote: Path, build_id: str, core_sha: str, nbytes: int) -> None:
    (remote / "manifest-set.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "assets": {"index.sqlite.gz": {"sha256": core_sha, "bytes": nbytes, "build_id": build_id}},
            }
        )
    )


def test_verify_on_accepts_consistent_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ERGON_VERIFY_SET_MANIFEST", "1")
    remote = tmp_path / "remote"
    remote.mkdir()
    raw, sha = _publish_core(remote, tmp_path, "b1")
    _write_set_manifest(remote, "b1", sha, len(raw))
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is not None  # consistent set -> normal download


def test_verify_on_rejects_torn_set_and_falls_back_to_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ERGON_VERIFY_SET_MANIFEST", "1")
    remote = tmp_path / "remote"
    remote.mkdir()
    cache_dir = tmp_path / "cache"

    # Prime the cache with a good b1 (consistent set), so a later torn build has a "prior good set".
    raw1, sha1 = _publish_core(remote, tmp_path, "b1")
    _write_set_manifest(remote, "b1", sha1, len(raw1))
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=cache_dir)
    assert cache.ensure_fresh() is not None  # cached at b1

    # Publish b2 but leave a TORN set manifest (its recorded core sha disagrees with manifest.json,
    # i.e. mid-upload: new manifest.json/gz, stale set manifest). Reader must fall back to b1.
    raw2, sha2 = _publish_core(remote, tmp_path, "b2")
    _write_set_manifest(remote, "b2", "0" * 64, len(raw2))  # wrong core sha
    path = cache.ensure_fresh()
    assert path is not None and path.exists()  # returned the cached prior, not None
    assert json.loads((cache_dir / "manifest.json").read_text())["build_id"] == "b1"  # stayed at b1


def test_verify_on_rejects_torn_set_no_cache_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ERGON_VERIFY_SET_MANIFEST", "1")
    remote = tmp_path / "remote"
    remote.mkdir()
    raw, sha = _publish_core(remote, tmp_path, "b1")
    _write_set_manifest(remote, "b1", "0" * 64, len(raw))  # torn
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # no cached prior -> live fallback


def test_verify_opt_out_ignores_torn_set(tmp_path, monkeypatch):
    # Explicit opt-out (=0) restores the pre-Item-6 unchecked path: a torn set manifest is never
    # cross-checked, so the download proceeds normally.
    monkeypatch.setenv("ERGON_VERIFY_SET_MANIFEST", "0")
    remote = tmp_path / "remote"
    remote.mkdir()
    raw, sha = _publish_core(remote, tmp_path, "b1")
    _write_set_manifest(remote, "b1", "0" * 64, len(raw))  # torn, but ignored while opted out
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is not None  # downloaded despite the torn set manifest


def test_verify_default_on_rejects_torn_set(tmp_path, monkeypatch):
    # GRADUATED: with NO env set, the verify is now ON by default -> a torn set manifest (core sha
    # disagrees) is rejected, falling back to the cached prior (None here, since none is cached).
    monkeypatch.delenv("ERGON_VERIFY_SET_MANIFEST", raising=False)
    remote = tmp_path / "remote"
    remote.mkdir()
    raw, sha = _publish_core(remote, tmp_path, "b1")
    _write_set_manifest(remote, "b1", "0" * 64, len(raw))  # torn: core sha != set-manifest sha
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # torn set rejected by default -> no cached prior


def test_verify_on_absent_set_manifest_behaves_as_before(tmp_path, monkeypatch):
    # A build that hasn't published a set manifest yet must not be penalized by an enabled flag.
    monkeypatch.setenv("ERGON_VERIFY_SET_MANIFEST", "1")
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_core(remote, tmp_path, "b1")  # no manifest-set.json written
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is not None
