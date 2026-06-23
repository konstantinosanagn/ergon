"""Stream B cron wiring: SpecHealth, TokenStore.ttl_remaining, tier2 refresh, spec-health check."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from spec_health_cron import check_specs  # noqa: E402
from tier2_refresh import needs_refresh, refresh  # noqa: E402

from ergon_tracker.spec_health import SpecHealth  # noqa: E402
from ergon_tracker.token_store import TokenStore  # noqa: E402

pytestmark = pytest.mark.anyio


# --- SpecHealth ----------------------------------------------------------------------------------
def test_spec_health_streak_and_reset(tmp_path):
    h = SpecHealth(tmp_path / "h.json")
    h.record("eog", False)
    h.record("eog", False)
    assert h.consecutive_failures("eog") == 2
    h.record("eog", True)  # a success resets the streak
    assert h.consecutive_failures("eog") == 0
    assert h.success_rate("eog") == pytest.approx(1 / 3)


def test_spec_health_stale_threshold_and_persist(tmp_path):
    h = SpecHealth(tmp_path / "h.json")
    for _ in range(3):
        h.record("dead", False)
    h.record("ok", True)
    assert h.is_stale("dead", threshold=3) and not h.is_stale("ok", threshold=3)
    assert h.stale(threshold=3) == ["dead"]
    h.save()
    assert SpecHealth(tmp_path / "h.json").consecutive_failures("dead") == 3  # persisted


# --- TokenStore.ttl_remaining --------------------------------------------------------------------
def test_ttl_remaining(tmp_path):
    t = [1000.0]
    s = TokenStore(tmp_path / "t.json", clock=lambda: t[0])
    assert s.ttl_remaining("k") is None  # absent
    s.set("k", "v", ttl_seconds=300)
    assert s.ttl_remaining("k") == pytest.approx(300)
    t[0] = 1290.0
    assert s.ttl_remaining("k") == pytest.approx(10)
    s.mark_stale("k")
    assert s.ttl_remaining("k") is None  # stale


# --- tier2 refresh decision ----------------------------------------------------------------------
def test_needs_refresh(tmp_path):
    t = [0.0]
    s = TokenStore(tmp_path / "t.json", clock=lambda: t[0])
    target = {"ttl_seconds": 100}
    assert needs_refresh(s, "k", target)  # missing -> refresh
    s.set("k", "v", ttl_seconds=100)
    t[0] = 50.0
    assert not needs_refresh(s, "k", target)  # 50s left of 100 (>20% margin)
    t[0] = 85.0
    assert needs_refresh(s, "k", target)  # 15s left (<20% margin) -> proactive refresh


async def test_refresh_runs_only_what_is_needed(tmp_path):
    t = [0.0]
    s = TokenStore(tmp_path / "t.json", clock=lambda: t[0])
    s.set("fresh", "v", ttl_seconds=1000)  # plenty of life -> skipped
    targets = {"fresh": {"ttl_seconds": 1000}, "expired": {"ttl_seconds": 1000}, "_README": {}}
    minted: list[str] = []

    async def fake_mint(ref):
        minted.append(ref)
        s.set(ref, "NEW", ttl_seconds=targets[ref]["ttl_seconds"])
        return "NEW"

    res = await refresh(s, targets, mint_fn=fake_mint)
    assert res["refreshed"] == ["expired"] and res["skipped"] == ["fresh"]
    assert minted == ["expired"]  # _README skipped, fresh skipped


# --- spec-health cron check ----------------------------------------------------------------------
async def test_check_specs_marks_failing_stale(tmp_path):
    h = SpecHealth(tmp_path / "h.json")

    async def fetch(token):
        if token == "good":
            return [{"id": 1}]
        if token == "boom":
            raise RuntimeError("network")
        return []  # "empty" -> 0 jobs -> failure

    stale = await check_specs(["good", "empty", "boom"], fetch, h, threshold=1)
    assert set(stale) == {"empty", "boom"} and "good" not in stale
