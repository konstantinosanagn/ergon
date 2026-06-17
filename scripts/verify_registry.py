"""Re-verify EVERY board in the seed registry against its live provider; report or prune dead.

``build_registry`` verifies candidates *at merge time*, but boards close over time and the
registry accumulates staleness — and not every entry necessarily passed the gate. This sweep
fetches every stored board through its real provider RIGHT NOW and reports (or, with
``--prune``, removes) the ones that no longer return any jobs.

Like the harvesters, it dogfoods the real provider stack and the shared ``AsyncFetcher``
(bounded concurrency + per-host rate + retries + circuit breaker). Pruning happens under the
same ``seed_lock`` ``build_registry`` uses, so it is safe alongside a concurrent merge.

Usage::

    .venv/bin/python scripts/verify_registry.py --sample 500          # quick health gauge
    .venv/bin/python scripts/verify_registry.py --ats greenhouse join  # one/few ATSes
    .venv/bin/python scripts/verify_registry.py --prune                # full sweep + remove dead
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_registry import seed_lock  # noqa: E402  (reuse the merge lock)
from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"


async def verify_one(
    key: str, entry: dict, fetcher: AsyncFetcher, query: SearchQuery
) -> tuple[str, bool, str | None]:
    """Return (company_key, is_live, error). Live = provider.fetch returns >=1 posting."""
    provider = get_provider(entry.get("ats", ""))
    token = entry.get("token")
    if provider is None or not token:
        return key, False, f"no provider/token for {entry.get('ats')}"
    try:
        raws = await provider.fetch(token, query, fetcher)
        return key, len(raws) > 0, None
    except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
        return key, False, f"{type(exc).__name__}: {exc}"[:80]


def select(companies: dict[str, dict], atses: list[str], sample: int | None) -> list[str]:
    """Pick the keys to verify: optional ATS filter, optional evenly-strided sample."""
    keys = [k for k, v in companies.items() if not atses or v.get("ats") in atses]
    if sample and sample < len(keys):
        stride = len(keys) / sample  # evenly spaced for a representative cross-section
        keys = [keys[int(i * stride)] for i in range(sample)]
    return keys


async def main() -> None:
    args = sys.argv[1:]
    prune = "--prune" in args
    sample: int | None = None
    atses: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sample":
            sample = int(args[i + 1]); i += 2
        elif a == "--ats":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                atses.append(args[i]); i += 1
        elif a == "--prune":
            i += 1
        else:
            print(f"unknown arg: {a}"); return

    load_builtins()
    seed = json.loads(SEED.read_text())
    companies: dict[str, dict] = seed["companies"]
    keys = select(companies, atses, sample)
    mode = "PRUNE" if prune else "report-only"
    scope = f"sample={sample}" if sample else (f"ats={atses}" if atses else "ALL")
    print(f"verifying {len(keys)} boards ({scope}, {mode}) of {len(companies)} total ...")

    query = SearchQuery()
    results: dict[int, tuple[str, bool, str | None]] = {}
    async with (
        AsyncFetcher(concurrency=16, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, key in enumerate(keys):
            async def run(idx: int = idx, key: str = key) -> None:
                results[idx] = await verify_one(key, companies[key], fetcher, query)

            tg.start_soon(run)

    live = [k for _, (k, ok, _) in sorted(results.items()) if ok]
    dead = [(k, err) for _, (k, ok, err) in sorted(results.items()) if not ok]

    by_ats_dead: dict[str, int] = {}
    for k, _ in dead:
        a = companies[k].get("ats", "?")
        by_ats_dead[a] = by_ats_dead.get(a, 0) + 1
    pct = (100 * len(dead) // len(keys)) if keys else 0
    print(f"\nlive={len(live)}  dead={len(dead)} ({pct}%)")
    print(f"dead by ats: {dict(sorted(by_ats_dead.items(), key=lambda x: -x[1]))}")
    if dead:
        print("dead samples:")
        for k, err in dead[:15]:
            print(f"  {companies[k].get('ats'):12s} {k:28s} {err}")

    if not prune:
        print("\nreport-only (no changes). Re-run with --prune to remove dead boards.")
        return
    if sample:
        print("\nrefusing to prune on a --sample run (would delete unverified boards). "
              "Run a full or per-ATS sweep with --prune.")
        return

    dead_keys = {k for k, _ in dead}
    with seed_lock():
        seed2 = json.loads(SEED.read_text())  # re-read under lock to compose with concurrent runs
        before = len(seed2["companies"])
        seed2["companies"] = {k: v for k, v in seed2["companies"].items() if k not in dead_keys}
        removed = before - len(seed2["companies"])
        SEED.write_text(json.dumps(seed2, indent=2, ensure_ascii=False) + "\n")
    print(f"\npruned {removed} dead boards -> {len(seed2['companies'])} live boards remain")


if __name__ == "__main__":
    anyio.run(main)
