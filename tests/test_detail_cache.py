from __future__ import annotations

import gzip
import hashlib
import json

from ergon_tracker.index.cache import DetailCache
from ergon_tracker.index.detail import DETAIL_SCHEMA_VERSION, open_detail


def _build_detail(tmp_path, *, name="detail.sqlite"):
    path = tmp_path / name
    con = open_detail(str(path))
    con.execute(
        "INSERT INTO job_detail(id, sig, fetched_at, attempts, snippet) VALUES (?, ?, ?, 0, ?)",
        ("j1", "sig1", "2026-01-01T00:00:00Z", "a snippet"),
    )
    con.commit()
    con.close()
    return path


def _publish_detail(remote, tmp_path, *, build_id="b1", name="detail.sqlite"):
    src = _build_detail(tmp_path, name=name)
    raw = src.read_bytes()
    (remote / "index-detail.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest-detail.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "schema_version": DETAIL_SCHEMA_VERSION,
            }
        )
    )
    return raw


def test_detail_cache_downloads_verifies_and_opens(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_detail(remote, tmp_path)
    path = DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh()
    assert path is not None and path.exists()
    con = open_detail(str(path))
    row = con.execute("SELECT id, snippet FROM job_detail WHERE id = 'j1'").fetchone()
    assert row == ("j1", "a snippet")


def test_detail_cache_absent_asset_returns_none(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()  # nothing published
    assert DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh() is None


def test_detail_cache_rejects_corrupt_download(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_detail(remote, tmp_path)
    m = json.loads((remote / "manifest-detail.json").read_text())
    m["sha256"] = "0" * 64
    (remote / "manifest-detail.json").write_text(json.dumps(m))
    assert DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh() is None


def test_detail_cache_warm_hit_does_not_redownload(tmp_path):
    # Cold download at build b1.
    remote = tmp_path / "remote"
    remote.mkdir()
    raw1 = _publish_detail(remote, tmp_path, build_id="b1", name="detail1.sqlite")
    cache = DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    path = cache.ensure_fresh()
    assert path is not None and path.read_bytes() == raw1

    # Swap the remote asset bytes for different content, but leave the manifest's build_id
    # unchanged (still "b1"). A real publish would never do this — it's here purely to prove,
    # by observing the filesystem, that a build_id match short-circuits before ever re-fetching
    # the .sqlite.gz: if the code re-downloaded regardless, the cached file would pick up this
    # new content instead of staying byte-identical to the first download.
    other = _build_detail(tmp_path, name="detail2.sqlite")
    con = open_detail(str(other))
    con.execute(
        "INSERT INTO job_detail(id, sig, fetched_at, attempts, snippet) VALUES (?, ?, ?, 0, ?)",
        ("j2", "sig2", "2026-01-02T00:00:00Z", "other snippet"),
    )
    con.commit()
    con.close()
    (remote / "index-detail.sqlite.gz").write_bytes(gzip.compress(other.read_bytes()))

    path2 = cache.ensure_fresh()
    assert path2 is not None and path2.read_bytes() == raw1  # untouched -> no re-download happened


def test_detail_cache_rejects_future_schema_version(tmp_path):
    # Forward-compat: when a future build bumps DETAIL_SCHEMA_VERSION, an older client must fall
    # back to no recovered detail (None), never crash trying to open a sidecar it can't read.
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_detail(remote, tmp_path)

    # Pin BOTH sides of the guard. Asserting only the reject would also pass a guard that rejects
    # every manifest (e.g. a misspelled key that always misses and falls through to the default)
    # -- so first prove the current version is accepted.
    ok = DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache-ok")
    assert ok.ensure_fresh() is not None  # current schema_version -> downloaded

    man = json.loads((remote / "manifest-detail.json").read_text())
    man["schema_version"] = DETAIL_SCHEMA_VERSION + 1  # newer than this client understands
    (remote / "manifest-detail.json").write_text(json.dumps(man))
    cache = DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # graceful fallback, no exception
    assert not cache.db_path.exists()  # never wrote/replaced the local db


def test_detail_cache_rejects_corrupt_asset_no_prior_cache(tmp_path):
    # Manifest is valid but the .sqlite.gz asset is corrupt (bad gzip / partial upload / 404
    # body). With no previously-cached good db to fall back to, ensure_fresh must return None and
    # must not raise or write a local db.
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_detail(remote, tmp_path)
    (remote / "index-detail.sqlite.gz").write_bytes(b"not-a-gzip-file")
    cache = DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # no exception, no fallback available
    assert not cache.db_path.exists()


def test_detail_cache_stale_build_id_triggers_redownload(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    raw1 = _publish_detail(remote, tmp_path, build_id="b1", name="detail1.sqlite")
    cache = DetailCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    path = cache.ensure_fresh()
    assert path is not None and path.read_bytes() == raw1

    other = _build_detail(tmp_path, name="detail2.sqlite")
    con = open_detail(str(other))
    con.execute(
        "INSERT INTO job_detail(id, sig, fetched_at, attempts, snippet) VALUES (?, ?, ?, 0, ?)",
        ("j2", "sig2", "2026-01-02T00:00:00Z", "other snippet"),
    )
    con.commit()
    con.close()
    raw2 = other.read_bytes()
    (remote / "index-detail.sqlite.gz").write_bytes(gzip.compress(raw2))
    (remote / "manifest-detail.json").write_text(
        json.dumps(
            {
                "build_id": "b2",
                "sha256": hashlib.sha256(raw2).hexdigest(),
                "bytes": len(raw2),
                "schema_version": DETAIL_SCHEMA_VERSION,
            }
        )
    )
    assert raw2 != raw1

    path2 = cache.ensure_fresh()
    assert path2 is not None and path2.read_bytes() == raw2  # picked up the new build
    manifest = json.loads((tmp_path / "cache" / "manifest-detail.json").read_text())
    assert manifest["build_id"] == "b2"
