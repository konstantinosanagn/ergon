"""Re-verify candidate ATS boards by dogfooding jobspine's own providers, then merge the
live ones into the seed registry.

This is both a verification gate and a concurrency stress test: every candidate is fetched
through the real provider stack, concurrently, bounded by the shared AsyncFetcher.

Usage:
    .venv/bin/python scripts/build_registry.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobspine.http import AsyncFetcher  # noqa: E402
from jobspine.models import SearchQuery  # noqa: E402
from jobspine.providers.base import get_provider, load_builtins  # noqa: E402

SEED = ROOT / "src" / "jobspine" / "registry" / "data" / "seed.json"
CANDIDATES = ROOT / "scripts" / "candidates.json"

# Lower = preferred when the same company verifies on multiple ATSes. Unknown ATSes sort last
# (via .get(..., 99)) so a new provider/candidate type can never KeyError this sweep.
ATS_PRIORITY = {
    "greenhouse": 0,
    "lever": 1,
    "ashby": 2,
    "workday": 3,
    "smartrecruiters": 4,
    "workable": 5,
    "recruitee": 6,
    "personio": 7,
}


def token_for(entry: dict) -> str:
    if entry["ats"] == "workday":
        return f"{entry['tenant']}|{entry['wd']}|{entry['site']}"
    return entry["token"]


async def verify_one(
    entry: dict, fetcher: AsyncFetcher, query: SearchQuery
) -> tuple[dict, int, str, str | None]:
    provider = get_provider(entry["ats"])
    token = token_for(entry)
    if provider is None:
        return entry, 0, token, f"no provider for {entry['ats']}"
    try:
        raws = await provider.fetch(token, query, fetcher)
        return entry, len(raws), token, None
    except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
        return entry, 0, token, f"{type(exc).__name__}: {exc}"[:100]


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    cand_path = Path(paths[0]) if paths else CANDIDATES
    load_builtins()
    candidates: list[dict] = json.loads(cand_path.read_text())
    query = SearchQuery()
    results: dict[int, tuple[dict, int, str, str | None]] = {}

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for i, entry in enumerate(candidates):

            async def run(i: int = i, entry: dict = entry) -> None:
                results[i] = await verify_one(entry, fetcher, query)

            tg.start_soon(run)

    verified: list[tuple[dict, int, str]] = []
    dead: list[tuple[dict, str | None]] = []
    for i in sorted(results):
        entry, count, token, err = results[i]
        if count > 0:
            verified.append((entry, count, token))
        else:
            dead.append((entry, err))

    # Dedup by company key; on conflict keep the most jobs, then best ATS priority.
    best: dict[str, tuple[dict, int, str]] = {}
    for entry, count, token in verified:
        key = entry["company"]
        cur = best.get(key)
        if cur is None or (count, -ATS_PRIORITY.get(entry["ats"], 99)) > (
            cur[1],
            -ATS_PRIORITY.get(cur[0]["ats"], 99),
        ):
            best[key] = (entry, count, token)

    seed = json.loads(SEED.read_text())
    companies: dict[str, dict] = seed["companies"]
    added = 0
    for key, (entry, _count, token) in sorted(best.items()):
        if key in companies:
            continue
        companies[key] = {
            "ats": entry["ats"],
            "token": token,
            "domain": entry.get("domain"),
        }
        added += 1

    seed["_meta"]["version"] = 2
    seed["_meta"]["updated"] = "2026-06-16"

    print(f"candidates={len(candidates)}  verified={len(verified)}  dead={len(dead)}")
    by_ats: dict[str, int] = {}
    for entry, _c, _t in best.values():
        by_ats[entry["ats"]] = by_ats.get(entry["ats"], 0) + 1
    print(f"unique verified by ats: {by_ats}")
    print(f"added={added}  registry_total={len(companies)}")
    if dead:
        print("\nDEAD (first 20):")
        for entry, err in dead[:20]:
            print(f"  {entry['ats']:10s} {entry['company']:25s} {err}")

    if dry_run:
        print("\n--dry-run: seed.json NOT written")
        return
    SEED.write_text(json.dumps(seed, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {SEED.relative_to(ROOT)}")


if __name__ == "__main__":
    anyio.run(main)
