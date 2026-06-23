"""Unit tests for the Tier-2 TokenStore (pure: injected clock + fake async mint, no browser/net)."""

from __future__ import annotations

import stat

import anyio
import pytest

from ergon_tracker.token_store import TokenStore

pytestmark = pytest.mark.anyio


def _store(tmp_path, t):
    return TokenStore(tmp_path / "tok.json", clock=lambda: t[0])


def test_get_absent_is_none(tmp_path):
    assert _store(tmp_path, [1000.0]).get("x") is None


def test_set_get_roundtrip_and_persist(tmp_path):
    t = [1000.0]
    s = _store(tmp_path, t)
    s.set("fastenal", "SECRET", ttl_seconds=600)
    assert s.get("fastenal") == "SECRET"
    # reload from disk -> still there
    assert TokenStore(tmp_path / "tok.json", clock=lambda: t[0]).get("fastenal") == "SECRET"


def test_ttl_expiry(tmp_path):
    t = [1000.0]
    s = _store(tmp_path, t)
    s.set("k", "v", ttl_seconds=300)
    t[0] = 1299.0
    assert s.get("k") == "v"  # still inside ttl
    t[0] = 1301.0
    assert s.get("k") is None  # expired


def test_ttl_none_never_expires_until_stale(tmp_path):
    t = [0.0]
    s = _store(tmp_path, t)
    s.set("k", "v", ttl_seconds=None)
    t[0] = 10**9
    assert s.get("k") == "v"
    s.mark_stale("k")
    assert s.get("k") is None


def test_mark_stale_forces_miss(tmp_path):
    s = _store(tmp_path, [1.0])
    s.set("k", "v", ttl_seconds=999)
    s.mark_stale("k")
    assert s.get("k") is None


def test_should_refresh_on(tmp_path):
    s = _store(tmp_path, [1.0])
    s.set("k", "v", ttl_seconds=999, refresh_on=(403,))
    assert s.should_refresh_on("k", 403) is True
    assert s.should_refresh_on("k", 401) is False  # not in this token's policy
    assert s.should_refresh_on("missing", 403) is True  # default policy


def test_file_is_secrets_grade_0600(tmp_path):
    s = _store(tmp_path, [1.0])
    s.set("k", "v", ttl_seconds=10)
    mode = stat.S_IMODE((tmp_path / "tok.json").stat().st_mode)
    assert mode == 0o600


def test_corrupt_file_behaves_empty(tmp_path):
    (tmp_path / "tok.json").write_text("{not json")
    assert TokenStore(tmp_path / "tok.json").get("k") is None


async def test_get_or_mint_mints_once_when_absent(tmp_path):
    s = _store(tmp_path, [1.0])
    calls = [0]

    async def mint():
        calls[0] += 1
        return "MINTED"

    assert await s.get_or_mint("k", mint, ttl_seconds=60) == "MINTED"
    assert await s.get_or_mint("k", mint, ttl_seconds=60) == "MINTED"  # cached -> no 2nd mint
    assert calls[0] == 1


async def test_get_or_mint_single_flight(tmp_path):
    # N concurrent callers for the same key must trigger exactly ONE mint (no browser stampede)
    s = _store(tmp_path, [1.0])
    calls = [0]

    async def mint():
        calls[0] += 1
        await anyio.sleep(0.02)  # hold the lock so others pile up behind it
        return "ONE"

    results: list[str] = []

    async def caller():
        results.append(await s.get_or_mint("k", mint, ttl_seconds=60))

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(caller)

    assert calls[0] == 1
    assert results == ["ONE"] * 8
