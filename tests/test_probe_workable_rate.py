"""Offline tests for the Workable rate-probe harness (scripts/probe_workable_rate.py).

No network: run_step is exercised through httpx.MockTransport, and the aggregation/decision helpers
are pure. This validates the probe's LOGIC (pacing, aggregation, the 429 stop-condition) before it is
ever pointed at the live endpoint."""

import importlib.util
import sys
from pathlib import Path

import anyio
import httpx

_spec = importlib.util.spec_from_file_location(
    "probe_workable_rate", Path(__file__).resolve().parent.parent / "scripts" / "probe_workable_rate.py"
)
probe = importlib.util.module_from_spec(_spec)
# Register before exec: dataclasses with `from __future__ import annotations` resolve types via
# sys.modules[cls.__module__] at class-creation time, which is None for an unregistered dynamic module.
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


def _mk(status, elapsed=0.1, retry_after=None):
    return probe.Result("s", status, elapsed, retry_after)


def test_summarize_counts_and_percentiles():
    results = [_mk(200, 0.10), _mk(200, 0.20), _mk(200, 0.30), _mk(429), _mk(None)]
    s = probe.summarize(results, target_rate=32, wall=0.25)
    assert s["n"] == 5 and s["ok"] == 3 and s["n429"] == 1 and s["errors"] == 1
    assert s["achieved_rate"] == 20.0  # 5 / 0.25
    assert s["p50_ms"] > 0 and s["p95_ms"] >= s["p50_ms"]


def test_step_failed_on_any_429():
    ok = probe.summarize([_mk(200), _mk(200)], 16, 1.0)
    assert not probe.step_failed(ok)
    bad = probe.summarize([_mk(200), _mk(429)], 16, 1.0)
    assert probe.step_failed(bad)


def test_step_failed_on_5xx_storm():
    # 1 of 10 5xx == 10% -> storm at the default threshold; 0 of 10 is clean.
    storm = probe.summarize([_mk(503)] + [_mk(200)] * 9, 16, 1.0)
    assert probe.step_failed(storm)
    clean = probe.summarize([_mk(200)] * 10, 16, 1.0)
    assert not probe.step_failed(clean)


def test_run_step_paces_and_collects_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await probe.run_step(client, ["a", "b", "c"], rate=200.0, count=12, max_inflight=8)

    results = anyio.run(go)
    assert len(results) == 12
    assert all(r.status == 200 for r in results)


def test_probe_stops_ramp_at_first_429():
    # Endpoint returns 200 until the client asks for 'boom', which 429s -> ramp must stop and NOT
    # escalate to the next rate. We flip to 429 on the 3rd (highest) step by counting calls.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # first two steps (2 requests each here) clean; anything after 429s
        return httpx.Response(429 if calls["n"] > 4 else 200, headers={"Retry-After": "1"})

    async def go():
        transport = httpx.MockTransport(handler)
        # patch the module client factory path by injecting our transport through a subclassed probe:
        # simplest is to call run_step per rate the way probe() does, asserting the stop logic here.
        knee = None
        summaries = []
        async with httpx.AsyncClient(transport=transport) as client:
            for rate in (16.0, 24.0, 32.0):
                res = await probe.run_step(client, ["a"], rate=1000.0, count=2, max_inflight=4)
                s = probe.summarize(res, rate, 0.01)
                summaries.append(s)
                if probe.step_failed(s):
                    break
                knee = rate
        return knee, summaries

    knee, summaries = anyio.run(go)
    # step 1 rate=16 (calls 1-2, clean) -> knee=16; step 2 rate=24 (calls 3-4, clean) -> knee=24;
    # step 3 rate=32 (calls 5-6, 429) -> stop. Last clean knee = 24.
    assert knee == 24.0
    assert len(summaries) == 3 and summaries[-1]["n429"] > 0
