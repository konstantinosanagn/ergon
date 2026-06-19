"""Download + verify + freshness-gate the published index snapshot.

Works two ways:
* **Anonymous** (public repo): fetch assets from the stable release download URL.
* **Token-auth** (private repo or rate-limit relief): if a GitHub token is present in the env
  (``ERGON_GH_TOKEN`` / ``GITHUB_TOKEN`` / ``GH_TOKEN``) and we're talking to github.com, resolve
  assets via the GitHub API and download them authenticated. This lets a private repo still serve
  the index to token-holders; a public repo needs no token (the intended zero-friction path).

Any failure returns the cached copy if present, else ``None`` (caller live-falls-back).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import SCHEMA_VERSION

log = logging.getLogger("ergon_tracker.index")
_REPO = "konstantinosanagn/ergon-tracker"
_TAG = "index-latest"
_DEFAULT_BASE = f"https://github.com/{_REPO}/releases/download/{_TAG}"


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "ergon-tracker"


def _token() -> str | None:
    return (
        os.environ.get("ERGON_GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
    )


def _get(url: str, *, token: str | None = None, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url)  # noqa: S310 - https/file only
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=60) as r:
        data: bytes = r.read()
        return data


def _asset_fetcher(base_url: str, repo: str, tag: str) -> Callable[[str], bytes]:
    """Return ``fetch(asset_name) -> bytes`` using token-auth API if available, else anonymous."""
    token = _token()
    if token and base_url.startswith("https://github.com"):
        api = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        rel = json.loads(_get(api, token=token, accept="application/vnd.github+json"))
        assets = {a["name"]: a["url"] for a in rel.get("assets", [])}
        return lambda name: _get(assets[name], token=token, accept="application/octet-stream")
    return lambda name: _get(f"{base_url}/{name}")


class IndexCache:
    def __init__(
        self,
        base_url: str | None = None,
        cache_dir: Path | None = None,
        repo: str = _REPO,
        tag: str = _TAG,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.cache_dir = Path(cache_dir or _default_cache_dir())
        self.repo = repo
        self.tag = tag
        self.db_path = self.cache_dir / "index.sqlite"
        self.local_manifest = self.cache_dir / "manifest.json"

    def _make_fetch(self) -> Callable[[str], bytes]:
        return _asset_fetcher(self.base_url, self.repo, self.tag)

    def _try_delta(
        self, fetch: Callable[[str], bytes], local_build_id: str, remote: dict[str, Any]
    ) -> Path | None:
        """Apply a row-level delta in place if one bridges local_build_id -> the remote build.

        Returns the updated db path on success, or None to fall back to a full download (no delta
        published, base mismatch, integrity failure, etc.). Never raises.
        """
        from .build import apply_delta

        try:
            dmeta = json.loads(fetch("manifest-delta.json"))
        except Exception as exc:  # noqa: BLE001 - no delta published -> full download
            log.debug("no delta manifest (%s); full download", exc)
            return None
        if dmeta.get("from_build_id") != local_build_id or dmeta.get("to_build_id") != remote.get(
            "build_id"
        ):
            # Not exactly one build behind — try chaining consecutive deltas (v2.2) before giving
            # up to a full download.
            return self._try_delta_chain(fetch, local_build_id, remote)
        try:
            raw = gzip.decompress(fetch("index-delta.sqlite.gz"))
        except Exception as exc:  # noqa: BLE001
            log.warning("delta download failed (%s); full download", exc)
            return None
        if hashlib.sha256(raw).hexdigest() != dmeta.get("sha256"):
            log.warning("delta sha256 mismatch; full download")
            return None
        delta_tmp = self.cache_dir / "index-delta.sqlite"
        delta_tmp.write_bytes(raw)
        try:
            apply_delta(self.db_path, delta_tmp)
        except Exception as exc:  # noqa: BLE001 - corrupt base/delta -> full download recovers
            log.warning("delta apply failed (%s); full download", exc)
            return None
        finally:
            delta_tmp.unlink(missing_ok=True)
        self.local_manifest.write_text(json.dumps(remote))
        log.info(
            "index updated via delta %s->%s (%d bytes)",
            local_build_id,
            remote.get("build_id"),
            len(raw),
        )
        return self.db_path

    def _try_delta_chain(
        self, fetch: Callable[[str], bytes], local_build_id: str, remote: dict[str, Any]
    ) -> Path | None:
        """Apply a CHAIN of consecutive deltas to catch up a base that's >1 build behind (v2.2).

        Reads the published ``deltas.json`` window, walks the from->to links from the local build to
        the remote build, and applies each in order via apply_delta (which re-checks ordering). Any
        gap, oversized chain, download/integrity/apply failure -> None (full download recovers).
        """
        from .build import apply_delta

        try:
            window = json.loads(fetch("deltas.json")).get("deltas", [])
        except Exception as exc:  # noqa: BLE001 - no window published -> full download
            log.debug("no deltas window (%s); full download", exc)
            return None
        by_from = {d["from_build_id"]: d for d in window}
        target = remote.get("build_id")
        chain: list[dict[str, Any]] = []
        cur = local_build_id
        seen: set[str] = set()
        while cur != target and cur in by_from and cur not in seen:
            seen.add(cur)
            step = by_from[cur]
            chain.append(step)
            cur = step["to_build_id"]
        if cur != target or not chain:
            log.debug("no contiguous delta chain local->remote; full download")
            return None
        # Only chain when it's actually cheaper than the full file.
        if sum(int(d.get("bytes", 0)) for d in chain) >= int(remote.get("bytes", 0) or 1 << 62):
            return None
        # Apply the chain to a COPY and swap atomically only on full success: each apply_delta
        # commits separately, so applying in place would leave the cached db half-advanced if a
        # later step fails. Working on a copy keeps the live db pristine for the full-download
        # fallback.
        import shutil

        work = self.db_path.with_suffix(".chain.sqlite")
        delta_tmp = self.cache_dir / "index-delta-chain.sqlite"
        try:
            shutil.copyfile(self.db_path, work)
            total = 0
            for step in chain:
                try:
                    raw = gzip.decompress(fetch(step["file"]))
                except Exception as exc:  # noqa: BLE001
                    log.warning("chain delta download failed (%s); full download", exc)
                    return None
                if hashlib.sha256(raw).hexdigest() != step.get("sha256"):
                    log.warning("chain delta sha mismatch; full download")
                    return None
                delta_tmp.write_bytes(raw)
                try:
                    apply_delta(work, delta_tmp)
                except Exception as exc:  # noqa: BLE001
                    log.warning("chain delta apply failed (%s); full download", exc)
                    return None
                total += len(raw)
            work.replace(self.db_path)  # atomic promote — only reached if every step succeeded
        finally:
            delta_tmp.unlink(missing_ok=True)
            work.unlink(missing_ok=True)
        self.local_manifest.write_text(json.dumps(remote))
        log.info(
            "index updated via %d-delta chain %s->%s (%d bytes)",
            len(chain),
            local_build_id,
            target,
            total,
        )
        return self.db_path

    def ensure_fresh(self) -> Path | None:
        """Return a verified, schema-compatible index path, or None (caller live-falls-back)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            fetch = self._make_fetch()
            remote = json.loads(fetch("manifest.json"))
        except Exception as exc:  # noqa: BLE001
            log.warning("index manifest fetch failed (%s); using cache if present", exc)
            return self.db_path if self.db_path.exists() else None
        if int(remote.get("schema_version", -1)) != SCHEMA_VERSION:
            log.warning("index schema_version mismatch; live fallback")
            return None
        local = json.loads(self.local_manifest.read_text()) if self.local_manifest.exists() else {}
        if local.get("build_id") == remote.get("build_id") and self.db_path.exists():
            return self.db_path  # already current
        # Incremental path: if we're exactly one build behind, a row-level delta is far smaller than
        # the whole file. Falls through to the full download on any miss (no delta, wrong base, etc.).
        local_build_id = local.get("build_id")
        if local_build_id and self.db_path.exists():
            delta_path = self._try_delta(fetch, str(local_build_id), remote)
            if delta_path is not None:
                return delta_path
        try:
            raw = gzip.decompress(fetch("index.sqlite.gz"))
        except Exception as exc:  # noqa: BLE001
            log.warning("index download failed (%s)", exc)
            return self.db_path if self.db_path.exists() else None
        if hashlib.sha256(raw).hexdigest() != remote.get("sha256"):
            log.warning("index sha256 mismatch; rejecting download")
            return None
        tmp = self.db_path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(self.db_path)  # atomic
        self.local_manifest.write_text(json.dumps(remote))
        log.info("index updated to build %s (%d bytes)", remote.get("build_id"), len(raw))
        return self.db_path


class SlimCache:
    """Download the compact slim broad-query tier (no snippet/years; ~half the full-file bytes).

    Same shape as IndexCache but for ``index-slim.sqlite.gz`` + ``manifest-slim.json``. Used for
    broad structured-filter queries that need no description text — a strict download-size win
    with identical results to the full index for those queries.
    """

    def __init__(
        self,
        base_url: str | None = None,
        cache_dir: Path | None = None,
        repo: str = _REPO,
        tag: str = _TAG,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.cache_dir = Path(cache_dir or _default_cache_dir())
        self.repo = repo
        self.tag = tag
        self.db_path = self.cache_dir / "index-slim.sqlite"
        self.local_manifest = self.cache_dir / "manifest-slim.json"

    def ensure_fresh(self) -> Path | None:
        """Return a verified slim index path, or None (caller falls back to full/live)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            fetch = _asset_fetcher(self.base_url, self.repo, self.tag)
            remote = json.loads(fetch("manifest-slim.json"))
        except Exception as exc:  # noqa: BLE001 - no slim published -> caller uses full index
            log.debug("no slim manifest (%s); falling back to full index", exc)
            return None
        if int(remote.get("schema_version", -1)) != SCHEMA_VERSION:
            return None
        local = json.loads(self.local_manifest.read_text()) if self.local_manifest.exists() else {}
        if local.get("build_id") == remote.get("build_id") and self.db_path.exists():
            return self.db_path  # already current
        try:
            raw = gzip.decompress(fetch("index-slim.sqlite.gz"))
        except Exception as exc:  # noqa: BLE001
            log.warning("slim download failed (%s)", exc)
            return self.db_path if self.db_path.exists() else None
        if hashlib.sha256(raw).hexdigest() != remote.get("sha256"):
            log.warning("slim sha256 mismatch; rejecting download")
            return None
        tmp = self.db_path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(self.db_path)  # atomic
        self.local_manifest.write_text(json.dumps(remote))
        log.info("slim index updated to build %s (%d bytes)", remote.get("build_id"), len(raw))
        return self.db_path


class ShardCache:
    """Download the shard manifest + only the shard(s) a query needs (v2 optimized path)."""

    def __init__(
        self,
        base_url: str | None = None,
        cache_dir: Path | None = None,
        repo: str = _REPO,
        tag: str = _TAG,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.repo, self.tag = repo, tag
        self.dir = Path(cache_dir or _default_cache_dir()) / "shards"
        self.manifest_path = self.dir / "shards.json"

    def _ensure_shard(self, info: dict[str, Any], fetch: Callable[[str], bytes]) -> bool:
        dest = self.dir / info["file"]
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == info["sha256"]:
            return True  # already cached for this build
        try:
            raw = gzip.decompress(fetch(info["file"] + ".gz"))
        except Exception as exc:  # noqa: BLE001
            log.warning("shard download failed (%s)", exc)
            return False
        if hashlib.sha256(raw).hexdigest() != info["sha256"]:
            log.warning("shard sha256 mismatch; rejecting")
            return False
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(dest)
        return True

    def ensure(self, query: Any) -> Path | None:
        """Ensure the manifest + needed shards are cached; return the shard dir, or None."""
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            fetch = _asset_fetcher(self.base_url, self.repo, self.tag)
            remote = json.loads(fetch("shards.json"))
        except Exception as exc:  # noqa: BLE001 - no shards published -> caller falls back
            # Expected on single-file deployments / before the first shard publish, not an
            # error: the caller transparently falls back to the single-file index.
            log.debug("no shard manifest (%s); falling back to single-file index", exc)
            return None
        if int(remote.get("schema_version", -1)) != SCHEMA_VERSION:
            return None
        shards = remote.get("shards", {})
        if not shards:
            return None
        from .build import sector_slug

        if query.sector:
            slug = sector_slug(query.sector)
            if slug not in shards:
                return None  # sector not present -> let caller try single-file/live
            needed = [slug]
        else:
            needed = list(shards)
        self.manifest_path.write_text(json.dumps(remote))  # full shard list (for the backend)
        for slug in needed:
            if not self._ensure_shard(shards[slug], fetch):
                return None
        return self.dir
