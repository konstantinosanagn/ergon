from __future__ import annotations

import gzip
import hashlib
import json

from tests.test_rich_index import FAKE, _build_rich, _job

from ergon_tracker.index.cache import RichCache
from ergon_tracker.index.rich import RICH_SCHEMA_VERSION, open_rich, vector_search


def _publish_rich(remote, tmp_path, *, build_id="b1", name="rich.sqlite", jobs=None):
    jobs = jobs or [_job("py", "Python Engineer", "python kubernetes")]
    src = _build_rich(tmp_path, jobs, name=name)
    raw = src.read_bytes()
    (remote / "index-vectors.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest-vectors.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "schema_version": RICH_SCHEMA_VERSION,
            }
        )
    )
    return raw


def test_rich_cache_downloads_verifies_and_opens(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_rich(remote, tmp_path)
    path = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh()
    assert path is not None and path.exists()
    con = open_rich(path)
    assert vector_search(con, FAKE.embed_query("python kubernetes"), limit=1)  # usable


def test_rich_cache_absent_asset_returns_none(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()  # nothing published
    assert RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh() is None


def test_rich_cache_rejects_corrupt_download(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_rich(remote, tmp_path)
    m = json.loads((remote / "manifest-vectors.json").read_text())
    m["sha256"] = "0" * 64
    (remote / "manifest-vectors.json").write_text(json.dumps(m))
    assert RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh() is None


def test_rich_cache_warm_hit_does_not_redownload(tmp_path):
    # Cold download at build b1.
    remote = tmp_path / "remote"
    remote.mkdir()
    raw1 = _publish_rich(remote, tmp_path, build_id="b1", name="rich1.sqlite")
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    path = cache.ensure_fresh()
    assert path is not None and path.read_bytes() == raw1

    # Swap the remote asset bytes for a DIFFERENT build's content, but leave the manifest's
    # build_id unchanged (still "b1"). A real publish would never do this — it's here purely to
    # prove, by observing the filesystem, that a build_id match short-circuits before ever
    # re-fetching the .sqlite.gz: if the code re-downloaded regardless, the cached file would pick
    # up this new content instead of staying byte-identical to the first download.
    other = _build_rich(tmp_path, [_job("sa", "Sales Rep", "sales quota")], name="rich2.sqlite")
    (remote / "index-vectors.sqlite.gz").write_bytes(gzip.compress(other.read_bytes()))

    path2 = cache.ensure_fresh()
    assert path2 is not None and path2.read_bytes() == raw1  # untouched -> no re-download happened


def test_rich_cache_rejects_future_schema_version(tmp_path):
    # Forward-compat: when a future build bumps RICH_SCHEMA_VERSION, an older client must fall
    # back to query-time reranking (None), never crash trying to open a sidecar it can't read.
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_rich(remote, tmp_path)
    man = json.loads((remote / "manifest-vectors.json").read_text())
    man["schema_version"] = RICH_SCHEMA_VERSION + 1  # newer than this client understands
    (remote / "manifest-vectors.json").write_text(json.dumps(man))
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # graceful fallback, no exception
    assert not cache.db_path.exists()  # never wrote/replaced the local db


def test_rich_cache_rejects_corrupt_asset_no_prior_cache(tmp_path):
    # Manifest is valid but the .sqlite.gz asset is corrupt (bad gzip / partial upload / 404 body).
    # With no previously-cached good db to fall back to, ensure_fresh must return None and must
    # not raise or write a local db.
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_rich(remote, tmp_path)
    (remote / "index-vectors.sqlite.gz").write_bytes(b"not-a-gzip-file")
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # no exception, no fallback available
    assert not cache.db_path.exists()


def test_rich_cache_stale_build_id_triggers_redownload(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    raw1 = _publish_rich(remote, tmp_path, build_id="b1", name="rich1.sqlite")
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    path = cache.ensure_fresh()
    assert path is not None and path.read_bytes() == raw1

    raw2 = _publish_rich(
        remote,
        tmp_path,
        build_id="b2",
        name="rich2.sqlite",
        jobs=[_job("sa", "Sales Rep", "sales quota")],
    )
    assert raw2 != raw1

    path2 = cache.ensure_fresh()
    assert path2 is not None and path2.read_bytes() == raw2  # picked up the new build
    manifest = json.loads((tmp_path / "cache" / "manifest-vectors.json").read_text())
    assert manifest["build_id"] == "b2"
