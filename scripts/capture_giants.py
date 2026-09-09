"""Playwright capture pass for the residual "proxied" giants.

Static careers-page HTML misses ATS hosts that the SPA injects at runtime (JPMorgan -> an
oraclecloud host, Microsoft -> eightfold). This loads each giant's careers page in headless
Chromium, captures every NETWORK REQUEST it fires, and reuses the comprehensive
``discover_giants.detect`` over those captured URLs — so the JS-injected ATS host is found and
turned into a token, then verified + name-adjudicated like the static pass.

Skips giants already recovered (sponsor core already seeded). Bounded browser concurrency.

Usage::

    .venv/bin/python scripts/capture_giants.py [--cap N] [--out scripts/candidates_giants.json]
    .venv/bin/python scripts/build_registry.py scripts/candidates_giants.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from discover_giants import (  # noqa: E402
    GIANTS,
    _candidate_urls,
    _company_key,
    detect,
)
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core, name_match  # noqa: E402

from census_successfactors import tavily  # noqa: E402  # isort: skip

from ergon.http import AsyncFetcher  # noqa: E402
from ergon.models import SearchQuery  # noqa: E402
from ergon.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
DEFAULT_OUT = ROOT / "scripts" / "candidates_giants.json"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


async def capture(urls: list[str], browser, limiter: anyio.CapacityLimiter) -> str:
    """Load up to 2 careers URLs in Chromium; return ALL network-request URLs as newline text
    (so ``detect`` — which regex-extracts URLs — works on it like page HTML)."""
    reqs: list[str] = []
    async with limiter:
        try:
            ctx = await browser.new_context(user_agent=_UA)
        except Exception:  # noqa: BLE001
            return ""
        try:
            for u in urls[:2]:
                try:
                    page = await ctx.new_page()
                    page.on("request", lambda r, sink=reqs: sink.append(r.url))
                    try:
                        await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(4500)  # let the SPA fire its data XHRs
                    except Exception:  # noqa: BLE001
                        pass
                    await page.close()
                except Exception:  # noqa: BLE001
                    continue
        finally:
            await ctx.close()
    return "\n".join(reqs)


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    cap = 400
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--cap":
            cap = int(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set.")
        return
    seed_keys = load_seed_keys()
    all_giants = json.loads(GIANTS.read_text())["uncovered_top"]
    # Residual = giants whose sponsor core isn't already a seed key (skip the ~50 we recovered).
    giants = [g for g in all_giants if _core(g["name"]) not in seed_keys][:cap]
    print(f"capture pass over {len(giants)} residual giants ...", flush=True)

    # Phase 1: Tavily careers URLs (concurrent).
    urls_by_idx: dict[int, list[str]] = {}

    async def find_urls(idx: int, g: dict, fetcher: AsyncFetcher) -> None:
        urls_by_idx[idx] = _candidate_urls(g["name"], await tavily(g["name"], key, fetcher))

    async with (
        AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=3) as tav,
        anyio.create_task_group() as tg,
    ):
        for idx, g in enumerate(giants):
            tg.start_soon(find_urls, idx, g, tav)

    # Phase 2: headless-browser network capture (bounded) -> detect on captured request URLs.
    hits: dict[int, tuple[str, str]] = {}
    done = [0]
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return

    async with (
        async_playwright() as p,
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=12.0, retries=1) as probe,
    ):
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(6)

        async def grab(idx: int, g: dict) -> None:
            captured = await capture(urls_by_idx.get(idx, []), browser, limiter)
            if captured:
                hit = await detect(g["name"], captured, probe)
                if hit:
                    hits[idx] = hit
            done[0] += 1
            if done[0] % 25 == 0:
                print(f"  captured {done[0]}/{len(giants)} (hits: {len(hits)})", flush=True)

        async with anyio.create_task_group() as tg:
            for idx, g in enumerate(giants):
                tg.start_soon(grab, idx, g)
        await browser.close()

    print(
        f"detected {len(hits)} {dict(Counter(a for a, _ in hits.values()))}; verifying ...",
        flush=True,
    )

    # Phase 3: verify + adjudicate + emit (same gate as the static pass).
    candidates: list[dict] = []
    taken: set[str] = set()

    async def verify(idx: int, ats: str, token: str, fetcher: AsyncFetcher) -> None:
        sponsor = giants[idx]["name"]
        ck = _company_key(sponsor, ats, token).lower()
        if not ck or ck in seed_keys or ck in taken:
            return
        try:
            raws = await get_provider(ats).fetch(token, SearchQuery(limit=1), fetcher)
        except Exception:  # noqa: BLE001
            raws = []
        board_co = raws[0].company if raws else ""
        ok = bool(raws) and (
            ats in ("workday", "eightfold", "oracle") or name_match(sponsor, board_co or "")
        )
        if ok:
            taken.add(ck)
            cand: dict = {
                "company": ck,
                "ats": ats,
                "domain": None,
                "_sponsor": sponsor,
                "_filings": giants[idx].get("filings"),
            }
            if ats == "workday":
                tenant, wd, site = token.split("|", 2)
                cand.update({"tenant": tenant, "wd": wd, "site": site})
            else:
                cand["token"] = token
            candidates.append(cand)

    async with (
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=25.0, retries=1) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, (ats, token) in hits.items():
            tg.start_soon(verify, idx, ats, token, fetcher)

    candidates.sort(key=lambda c: -(c.get("_filings") or 0))
    for c in candidates:
        tok = c.get("token") or f"{c.get('tenant', '')}|{c.get('wd', '')}|{c.get('site', '')}"
        print(f"  + {c['_sponsor'][:28]:28} ({c.get('_filings') or 0:>5}) {c['ats']:12} {tok}")
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(
        f"\nwrote {len(out)} candidates {dict(Counter(c['ats'] for c in candidates))}"
        f" -> {out_path.name}"
    )


if __name__ == "__main__":
    anyio.run(main)
