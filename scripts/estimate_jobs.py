"""Estimate total live jobs across the registry by stratified sampling.

Per ATS: sample N companies, get each company's TRUE open-job count (one cheap request where
possible), take the mean, multiply by that ATS's company count. Workday/SmartRecruiters expose
an exact total field; others return the full board so len() is exact. Aggregators are reported
separately (their feeds are bounded, not per-company).
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
AGGREGATORS = {"remoteok", "remotive", "arbeitnow", "jobicy", "himalayas", "themuse"}
SAMPLE_PER_ATS = 25


async def company_total(ats: str, token: str, fetcher: AsyncFetcher) -> int | None:
    """True open-job count for one company (cheapest method per ATS)."""
    try:
        if ats == "workday":
            tenant, wd, site = token.split("|")
            url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            d = await fetcher.post_json(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            return int(d.get("total") or 0)
        if ats == "smartrecruiters":
            d = await fetcher.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1"
            )
            return int(d.get("totalFound") or 0)
        provider = get_provider(ats)
        if provider is None:
            return None
        raws = await provider.fetch(token, SearchQuery(), fetcher)
        return len(raws)
    except Exception:  # noqa: BLE001
        return None


async def main() -> None:
    load_builtins()
    seed = json.loads(SEED.read_text())["companies"]
    by_ats: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, v in seed.items():
        by_ats[v["ats"]].append((key, v["token"]))

    rng = random.Random(17)
    results: dict[str, list[int]] = defaultdict(list)
    counts: dict[str, int] = {a: len(v) for a, v in by_ats.items()}

    async with AsyncFetcher(concurrency=12, per_host_rate=6, timeout=30.0) as fetcher:
        for ats, comps in by_ats.items():
            sample = rng.sample(comps, min(SAMPLE_PER_ATS, len(comps)))
            async with anyio.create_task_group() as tg:
                for key, token in sample:

                    async def one(ats: str = ats, token: str = token) -> None:
                        n = await company_total(ats, token, fetcher)
                        if n is not None:
                            results[ats].append(n)

                    tg.start_soon(one)

    print(f"{'ATS':16s} {'companies':>9s} {'sampled':>7s} {'mean/co':>8s} {'est. jobs':>10s}")
    grand = 0
    for ats in sorted(counts, key=lambda a: -counts[a]):
        samp = results.get(ats, [])
        mean = sum(samp) / len(samp) if samp else 0.0
        est = int(mean * counts[ats])
        grand += est
        print(f"{ats:16s} {counts[ats]:>9d} {len(samp):>7d} {mean:>8.1f} {est:>10d}")
    print(f"\nESTIMATED ATS JOBS (extrapolated): ~{grand:,}")
    print(f"registry companies: {sum(counts.values()):,}")


if __name__ == "__main__":
    t = time.time()
    anyio.run(main)
