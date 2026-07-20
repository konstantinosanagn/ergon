"""Spec-health cron — replay every apicapture spec, record health, emit the re-discover queue.

The self-healing half of Stream B: browser-discovered specs rot when a site changes its API shape.
This cron replays each spec through the real provider, records ok/fail in the
:class:`~ergon_tracker.spec_health.SpecHealth` tracker, and writes ``runs/rediscover_queue.json`` =
the specs that have failed ``threshold`` times in a row — the input for a re-discovery pass.

Runs on the index-build cron (after the build). ``check_specs`` is pure over an injected ``fetch``
(unit-tested); ``main`` wires the live apicapture provider.

Usage::
    python scripts/spec_health_cron.py            # health-check all apicapture specs
    python scripts/spec_health_cron.py --threshold 2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.spec_health import DEFAULT_STALE_THRESHOLD, SpecHealth  # noqa: E402

# Persisted across builds so the consecutive-failure streak survives (the cron downloads+re-uploads
# it like board_state.json). $ERGON_SPEC_HEALTH points it at the build's dist/ in CI.
HEALTH_PATH = Path(os.environ.get("ERGON_SPEC_HEALTH") or (ROOT / "runs" / "spec_health.json"))
REDISCOVER_QUEUE = HEALTH_PATH.parent / "rediscover_queue.json"


async def check_specs(
    tokens: list[str],
    fetch: Callable[[str], Awaitable[list]],
    health: SpecHealth,
    *,
    threshold: int = DEFAULT_STALE_THRESHOLD,
    now: str | None = None,
) -> list[str]:
    """Replay each token via ``fetch``; ok = it returned ≥1 job. Returns the stale (re-discover) list."""
    for token in tokens:
        try:
            ok = len(await fetch(token)) > 0
        except Exception:  # noqa: BLE001 - any replay error is a failure for health purposes
            ok = False
        health.record(token, ok, now=now)
    health.save()
    return health.stale(threshold)


def main() -> None:
    import anyio

    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.apicapture import ApiCaptureProvider, _load_specs

    ap = argparse.ArgumentParser(description="apicapture spec health-check + re-discover queue")
    ap.add_argument("--threshold", type=int, default=DEFAULT_STALE_THRESHOLD)
    args = ap.parse_args()

    tokens = [t for t in _load_specs() if not t.startswith("_")]
    health = SpecHealth(HEALTH_PATH)

    async def fetch(token: str) -> list:
        async with AsyncFetcher(timeout=25) as f:
            return await ApiCaptureProvider().fetch(token, SearchQuery(limit=5), f)

    # anyio.run(func, *args, **kw) does NOT forward kwargs to `func` -- it consumes them itself
    # (backend=/backend_options=) and raises TypeError for anything else (e.g. TypeError: run() got
    # an unexpected keyword argument 'threshold'). Wrap in a lambda so `threshold` actually reaches
    # check_specs.
    stale = anyio.run(lambda: check_specs(tokens, fetch, health, threshold=args.threshold))
    REDISCOVER_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    REDISCOVER_QUEUE.write_text(json.dumps(stale, indent=1))
    healthy = len(tokens) - len(stale)
    print(
        f"checked {len(tokens)} specs | healthy={healthy} | stale={len(stale)} -> "
        f"{REDISCOVER_QUEUE.relative_to(ROOT)}"
    )
    if stale:
        print(f"  re-discover: {stale}", file=sys.stderr)


if __name__ == "__main__":
    main()
