"""Focused Playwright own-domain-API capture for a curated list of big residual corporations
whose careers pages are JS-rendered (no statically-extractable ATS host) but which call a public
no-auth JSON API on their OWN domain.

These specific corps survived every static method (slug-probe, Tavily-snippet Workday [poison],
relation-guarded Workday, authoritative careers-page extraction, Greenhouse board API — all 0).
The remaining lever is executing the page's JS and watching for a same-domain job-records request.
Reuses capture_api's hardened ``infer_spec`` (the ``_host_relates`` guard rejects the third-party
widgets/aggregators that poisoned the broad brand-normalized pass) + the apicapture provider.

Usage::

    .venv/bin/python scripts/capture_corps.py
    .venv/bin/python scripts/build_registry.py scripts/candidates_corps.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from capture_api import capture  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
SPECS = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "apicapture.json"
OUT = ROOT / "scripts" / "candidates_corps.json"

# (giant company key, brand name passed to infer_spec's host-relation guard, careers URLs to load)
CORPS = [
    ("twosigmainvestments", "two sigma", ["https://careers.twosigma.com/"]),
    ("depositorytrustandclearing", "dtcc", ["https://careers.dtcc.com/"]),
    ("fujitsunorthamerica", "fujitsu", ["https://careers.fujitsu.com/en"]),
    ("schlumbergertechnology", "schlumberger slb", ["https://careers.slb.com/"]),
    ("creditkarma", "credit karma", ["https://careers.creditkarma.com/us/en"]),
    ("slalom", "slalom", ["https://careers.slalom.com/en-us/"]),
    ("tiktokusdatasecurity", "tiktok usds", ["https://careers.usds.tiktok.com/"]),
    ("twosigmainvestments", "two sigma", ["https://careers.twosigma.com/jobs"]),
]


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return

    specs: dict[str, dict] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(4)

        async def grab(ck: str, brand: str, urls: list[str]) -> None:
            if ck in specs:
                return
            spec = await capture(brand, urls, browser, limiter)
            if spec:
                specs[ck] = spec
                print(f"  captured {ck:28} {spec['method']:4} {spec['url'][:56]}", flush=True)
            else:
                print(f"  ---      {ck:28} no same-domain job API", flush=True)

        async with anyio.create_task_group() as tg:
            for ck, brand, urls in CORPS:
                tg.start_soon(grab, ck, brand, urls)
        await browser.close()

    if not specs:
        print("no specs captured.")
        return

    # Verify each captured spec actually fetches jobs before writing it into the data file.
    existing = json.loads(SPECS.read_text()) if SPECS.exists() else {}
    candidates: list[dict] = []
    async with AsyncFetcher(concurrency=6, per_host_rate=4, timeout=25.0, retries=1) as f:
        for ck, spec in specs.items():
            existing[ck] = spec  # provider reads specs by token from the data file
            SPECS.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
            try:
                raws = await get_provider("apicapture").fetch(ck, SearchQuery(limit=10), f)
            except Exception:  # noqa: BLE001
                raws = []
            n = len(raws)
            print(f"  verify {ck:28} -> {n} jobs")
            if raws:
                candidates.append({"company": ck, "ats": "apicapture", "domain": None, "token": ck})
            else:
                del existing[ck]  # drop a spec that doesn't actually return jobs
                SPECS.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")

    OUT.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(candidates)} verified corp candidates -> {OUT.name}")


if __name__ == "__main__":
    anyio.run(main)
