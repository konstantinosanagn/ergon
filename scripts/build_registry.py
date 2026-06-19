"""Re-verify candidate ATS boards by dogfooding ergon_tracker's own providers, then merge the
live ones into the seed registry.

This is both a verification gate and a concurrency stress test: every candidate is fetched
through the real provider stack, concurrently, bounded by the shared AsyncFetcher.

Usage:
    .venv/bin/python scripts/build_registry.py [--dry-run]
"""

from __future__ import annotations

import fcntl
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
CANDIDATES = ROOT / "scripts" / "candidates.json"
_SEED_LOCK = SEED.with_name(SEED.name + ".lock")


@contextmanager
def seed_lock():
    """Serialize the seed read-modify-write across concurrent build_registry runs.

    Verification (the network-heavy part) runs *before* this and stays fully concurrent; only
    the short read-merge-write critical section is mutually exclusive, via an advisory flock on
    a sidecar lockfile. A second run blocks here, then reads the seed *after* the first run's
    write, so additions compose instead of clobbering.
    """
    with open(_SEED_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

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
    "bamboohr": 8,
    "breezy": 9,
    "teamtailor": 10,
    "join": 11,
    "rippling": 12,
    "pinpoint": 13,
    "eightfold": 14,
    "successfactors": 15,
    "oracle": 16,
    "jobdiva": 16.5,  # authoritative staffing/IT-firm candidate portal (own JSON API)
    "ripplehire": 16.6,  # IT-services career-site ATS (own XML API): Mphasis, CitiusTech
    "zwayam": 16.7,  # Indian IT-services ATS (public.zwayam.com 2-step ES API): Tavant
    "ceipal": 16.8,  # dominant US/Indian IT-staffing ATS (careerapi.ceipal.com referer-gated API)
    "radancy": 16.9,  # Radancy/TalentBrew /search-jobs server-rendered board: PwC, Carnival
    "taleo": 17,
    "taleobe": 17.5,  # Taleo Business Edition (CwsV2 HTML)
    "icims": 18,
    "avature": 19,
    "jazzhr": 20,
    "jobvite": 21,
    "phenom": 22,
    "brassring": 23,
    "schemaorg": 24,  # generic fallback (sitemap/JSON-LD) — lowest priority vs a real ATS
    "apicapture": 25,  # captured own-domain JSON/GraphQL API (proxied giants)
    "coveo": 26,
    "peopleadmin": 27,
    "peopleclick": 27.5,  # PeopleFluent candidate portal (cookie-primed JSON; partial)  # higher-ed/public-sector Atom feed (complete)  # Coveo-for-Sitecore same-origin job proxy (SLB etc.)
    "usajobs": 28,
    "dejobs": 28.5,  # DirectEmployers federation (recruiter-direct, company-filtered)  # authoritative federal-agency board (Organization code) — gov giants only
    "themuse": 29,  # curated aggregator company board (employer-matched) — for unreachable giants
    "adzuna": 30,  # aggregator fallback for truly-proxied giants (lowest priority)
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
    # Verification only needs to confirm a board returns >=1 job — fetching every page (up to a
    # provider's MAX_PAGES) just to gate-check is pure waste and lets one huge board stall the
    # whole sweep. Cap to the first page; the dedup tiebreaker only needs a live signal, not an
    # exact count.
    query = SearchQuery(limit=5)
    results: dict[int, tuple[dict, int, str, str | None]] = {}

    total = len(candidates)
    prog = {"done": 0, "live": 0}

    def tick(is_live: bool) -> None:
        prog["done"] += 1
        prog["live"] += int(is_live)
        d = prog["done"]
        # Stream progress every ~2% (min 100) so long sweeps aren't a silent black box.
        step = max(100, total // 50)
        if d % step == 0 or d == total:
            pct = 100 * d // total if total else 100
            print(f"  verifying {d}/{total} ({pct}%)  live={prog['live']} "
                  f"dead={d - prog['live']}", flush=True)

    print(f"verifying {total} candidates ...", flush=True)
    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for i, entry in enumerate(candidates):

            async def run(i: int = i, entry: dict = entry) -> None:
                res = await verify_one(entry, fetcher, query)
                results[i] = res
                tick(res[1] > 0)

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

    # Read-merge-write under the lock so concurrent runs compose instead of clobbering.
    with seed_lock():
        seed = json.loads(SEED.read_text())
        companies: dict[str, dict] = seed["companies"]
        added = 0
        for key, (entry, _count, token) in sorted(best.items()):
            # Registry keys are always lowercase; the token keeps its case (some ATSes, e.g.
            # SmartRecruiters, are case-sensitive on the token but not the company key).
            lk = key.lower()
            if lk in companies:
                continue
            companies[lk] = {
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
