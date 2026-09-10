"""A downloaded manifest must never decide where we write on disk.

``ShardCache._ensure_shard`` joined ``info["file"]`` — a value from the downloaded
``shards.json`` — straight onto the cache directory. A manifest entry of
``{"file": "../../../.zshrc"}`` therefore replaced that file with attacker-controlled bytes, and
the method returned ``True``. Reproduced before the fix.

The sha256 beside it authorizes nothing: payload and hash come from the SAME manifest, so an
attacker who can write the release controls both sides of that comparison. It detects a corrupted
transfer, not a malicious publisher. That is why the filename itself has to be constrained.

The exposure needs control of the GitHub Release assets, so it is not remotely exploitable on its
own — it is what turns "release compromise" into "code execution on every consumer", by way of
a shell rc file.
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest

from ergon.index.cache import ShardCache, safe_shard_name

_PAYLOAD = b"attacker-controlled bytes\n"
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()

# Every shape that tries to leave the cache directory, plus names that merely fail the expected
# pattern. Rejection is by allow-list, so both classes are refused.
_HOSTILE = [
    "../../victim",
    "../../../.zshrc",
    "/etc/passwd",
    "/tmp/absolute",
    "shard-x/../../../victim",
    "..\\..\\victim",
    "shard-ok.sqlite/../../victim",
    "shard-ok.sqlite\x00.png",  # NUL truncation
    "shard-.sqlite",  # empty slug
    "shard-UP.sqlite",  # slugs are lowercase
    "shard-ok.db",  # wrong extension
    "sha rd-ok.sqlite",  # whitespace
    "shard-ok.sqlite.gz",  # the compressed name, not the db
    "",
    ".",
    "..",
]

_LEGITIMATE = [
    "shard-unknown.sqlite",
    "shard-healthcare.sqlite",
    "shard-e-commerce-retail.sqlite",
    "shard-realestate-proptech.sqlite",
    "shard-ai-ml.sqlite",
    "shard-crypto-web3.sqlite",
]


@pytest.mark.parametrize("name", _HOSTILE)
def test_hostile_shard_name_is_rejected(name: str) -> None:
    assert safe_shard_name(name) is None, f"{name!r} should not be an accepted shard filename"


@pytest.mark.parametrize("name", _LEGITIMATE)
def test_real_shard_names_still_accepted(name: str) -> None:
    """Guard the guard: an over-tight pattern that rejects real shards breaks sector search."""
    assert safe_shard_name(name) == name


def test_non_string_is_rejected() -> None:
    for value in (None, 123, ["shard-x.sqlite"], {"file": "shard-x.sqlite"}):
        assert safe_shard_name(value) is None


def test_traversal_does_not_write_outside_the_cache(tmp_path: Path) -> None:
    """The end-to-end PoC, kept as a regression: the victim file must survive untouched."""
    victim = tmp_path / "victim"
    original = "# the user's real file\n"
    victim.write_text(original)

    cache = ShardCache(cache_dir=str(tmp_path / "cache"))
    cache.dir.mkdir(parents=True, exist_ok=True)

    for name in ("../victim", "../../victim", str(victim)):
        accepted = cache._ensure_shard(
            {"file": name, "sha256": _SHA}, lambda _n: gzip.compress(_PAYLOAD)
        )
        assert accepted is False, f"{name!r} was accepted"

    assert victim.read_text() == original, "a manifest entry rewrote a file outside the cache"


def test_a_legitimate_shard_still_lands_in_the_cache(tmp_path: Path) -> None:
    cache = ShardCache(cache_dir=str(tmp_path / "cache"))
    cache.dir.mkdir(parents=True, exist_ok=True)
    assert cache._ensure_shard(
        {"file": "shard-healthcare.sqlite", "sha256": _SHA},
        lambda _n: gzip.compress(_PAYLOAD),
    )
    assert (cache.dir / "shard-healthcare.sqlite").read_bytes() == _PAYLOAD


def test_the_fetched_asset_name_is_the_validated_one(tmp_path: Path) -> None:
    """The download URL must be built from the validated name, not the raw manifest value."""
    cache = ShardCache(cache_dir=str(tmp_path / "cache"))
    cache.dir.mkdir(parents=True, exist_ok=True)
    asked: list[str] = []

    def fetch(name: str) -> bytes:
        asked.append(name)
        return gzip.compress(_PAYLOAD)

    cache._ensure_shard({"file": "shard-fintech.sqlite", "sha256": _SHA}, fetch)
    assert asked == ["shard-fintech.sqlite.gz"]
