"""Stream B cron wiring: SpecHealth, TokenStore.ttl_remaining, tier2 refresh, spec-health check."""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
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


# --- anyio.run() kwarg-forwarding regression (2026-06-22 .. 2026-07-19 silent no-op) --------------
# anyio.run(func, *args, **kw) does NOT forward kw to `func` -- it treats any kwarg as its own
# (backend=/backend_options=) and raises TypeError for anything else. Both cron `main()`s used to
# call it as `anyio.run(coro_fn, *args, some_kwarg=x)`, which always raised TypeError; the
# fault-tolerant workflow step swallowed it, so the stale-threshold / margin-frac knobs silently
# never reached the coroutine. Fixed by wrapping in a lambda. These are sync tests (not
# `async def`) so pytest-anyio leaves them alone -- they drive their own event loop via
# `anyio.run`, exactly like the two scripts' `main()` do.
def test_anyio_run_direct_kwarg_raises_typeerror(tmp_path):
    """Documents the exact bug: the old direct-kwarg call form always raises TypeError."""
    h = SpecHealth(tmp_path / "h.json")

    async def fetch(token):
        return [{"id": 1}]

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        anyio.run(check_specs, ["x"], fetch, h, threshold=1)  # old spec_health_cron.py:73 shape

    t = [0.0]
    s = TokenStore(tmp_path / "t.json", clock=lambda: t[0])
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        anyio.run(refresh, s, {}, None, margin_frac=0.2)  # old tier2_refresh.py:84 shape


def test_anyio_run_lambda_delivers_threshold_to_check_specs(tmp_path):
    """The fixed spec_health_cron.py:73 pattern: threshold must actually reach check_specs."""
    h = SpecHealth(tmp_path / "h.json")

    async def fetch(token):
        return [] if token == "flaky" else [{"id": 1}]

    # threshold=1 -> a single failure is enough to be flagged stale.
    stale = anyio.run(lambda: check_specs(["flaky", "good"], fetch, h, threshold=1))
    assert stale == ["flaky"]

    h2 = SpecHealth(tmp_path / "h2.json")
    # threshold=5 -> one failure is NOT enough; proves the kwarg (not some hardcoded default)
    # is what's driving the result.
    stale2 = anyio.run(lambda: check_specs(["flaky", "good"], fetch, h2, threshold=5))
    assert stale2 == []


def test_anyio_run_lambda_delivers_margin_frac_to_refresh(tmp_path):
    """The fixed tier2_refresh.py:84 pattern: margin_frac must actually reach refresh()."""
    t = [0.0]
    s = TokenStore(tmp_path / "t.json", clock=lambda: t[0])
    s.set("k", "v", ttl_seconds=100)
    t[0] = 85.0  # 15s (15%) of TTL remains
    targets = {"k": {"ttl_seconds": 100}}
    minted: list[str] = []

    async def fake_mint(ref):
        minted.append(ref)
        return "NEW"

    # margin_frac=0.1 -> 15% remaining is ABOVE the 10% margin -> skipped.
    res = anyio.run(lambda: refresh(s, targets, None, margin_frac=0.1, mint_fn=fake_mint))
    assert res["skipped"] == ["k"] and minted == []

    # margin_frac=0.5 -> 15% remaining is BELOW the 50% margin -> refreshed. Only the kwarg
    # changed, so this proves margin_frac is what's driving the decision (not a hardcoded default).
    res2 = anyio.run(lambda: refresh(s, targets, None, margin_frac=0.5, mint_fn=fake_mint))
    assert res2["refreshed"] == ["k"] and minted == ["k"]


# --- main()-level integration: drives the exact anyio.run() call at spec_health_cron.py:73 and
# tier2_refresh.py:84 through real `main()`, with only network/CLI edges stubbed. This is the
# strongest regression guard: it fails with the pre-fix TypeError and passes post-fix.
def test_spec_health_cron_main_wires_threshold(tmp_path, monkeypatch):
    import json as _json

    import spec_health_cron

    import ergon_tracker.http as http_mod
    from ergon_tracker.providers import apicapture as apicapture_mod

    monkeypatch.setattr(spec_health_cron, "ROOT", tmp_path)  # so main()'s relative_to() log works
    monkeypatch.setattr(spec_health_cron, "HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(spec_health_cron, "REDISCOVER_QUEUE", tmp_path / "queue.json")
    monkeypatch.setattr(apicapture_mod, "_load_specs", lambda: ["flaky", "good"])

    async def fake_fetch(self, token, query, fetcher):
        return [] if token == "flaky" else [{"id": 1}]

    monkeypatch.setattr(apicapture_mod.ApiCaptureProvider, "fetch", fake_fetch)

    class _FakeAsyncFetcher:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(http_mod, "AsyncFetcher", _FakeAsyncFetcher)
    monkeypatch.setattr(sys, "argv", ["spec_health_cron.py", "--threshold", "1"])

    spec_health_cron.main()  # pre-fix: raises TypeError here (kwarg not forwarded by anyio.run)

    queue = _json.loads((tmp_path / "queue.json").read_text())
    assert queue == ["flaky"]  # threshold=1 reached check_specs and drove the real result


def test_tier2_refresh_main_wires_margin_frac(tmp_path, monkeypatch):
    import tier2_refresh

    from ergon_tracker.token_store import TokenStore as _RealTokenStore

    t = [0.0]
    store_path = tmp_path / "tokens.json"
    seed = _RealTokenStore(store_path, clock=lambda: t[0])
    seed.set("k", "v", ttl_seconds=100)
    t[0] = 85.0  # 15% of TTL remains

    class _FixedClockStore(_RealTokenStore):
        def __init__(self, path, **kw):
            super().__init__(path, clock=lambda: t[0], **kw)

    monkeypatch.setattr(tier2_refresh, "TokenStore", _FixedClockStore)
    monkeypatch.setattr(tier2_refresh, "_load_targets", lambda: {"k": {"ttl_seconds": 100}})

    minted: list[str] = []

    async def fake_mint(ref, store, targets):
        minted.append(ref)
        return "NEW"

    monkeypatch.setattr(tier2_refresh, "mint", fake_mint)

    # margin=0.1 -> 15% remaining is ABOVE the 10% margin -> nothing refreshed, exits 0.
    monkeypatch.setattr(
        sys, "argv", ["tier2_refresh.py", "--store", str(store_path), "--margin", "0.1"]
    )
    with pytest.raises(SystemExit) as exc:
        tier2_refresh.main()  # pre-fix: raises TypeError here, not SystemExit
    assert exc.value.code == 0
    assert minted == []

    # margin=0.5 -> 15% remaining is BELOW the 50% margin -> refreshed. Only the kwarg changed.
    monkeypatch.setattr(
        sys, "argv", ["tier2_refresh.py", "--store", str(store_path), "--margin", "0.5"]
    )
    with pytest.raises(SystemExit) as exc2:
        tier2_refresh.main()
    assert exc2.value.code == 0
    assert minted == ["k"]
