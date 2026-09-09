"""Capture real Eightfold tenant slugs for sponsors whose slug != company name.

Some Eightfold sponsors (AMD, Deere, UBS, Nomura, Activision...) host their board on a
NON-OBVIOUS tenant slug, so a name-guess (``amd`` -> ``amd.eightfold.ai``) fails to resolve
even though the board exists. The slug only appears in the careers SPA's *network traffic*.

This is a slug-DISCOVERY pass (not an access workaround — the PCSX provider already handles
access). Per sponsor:

  1. Tavily-search ``{sponsor} careers`` (aggregators excluded) for the company's OWN careers URL.
  2. Load that URL in headless Chromium and capture the ``{slug}.eightfold.ai`` host its XHRs hit.
  3. Verify the captured slug live through EightfoldProvider (PCSX-capable). Only slugs that
     return >=1 job and aren't already seeded are emitted as build_registry candidates.

Provenance is the careers domain: whatever Eightfold tenant the sponsor's own careers page calls
is theirs, so no fuzzy name-adjudication is needed — just the live verify gate.

Usage::

    .venv/bin/python scripts/capture_eightfold_slugs.py --gap-file runs/gap_eightfold.json \
        --out scripts/candidates_ef_slugs.json [--cap 30]
    .venv/bin/python scripts/build_registry.py scripts/candidates_ef_slugs.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

from ergon.http import AsyncFetcher  # noqa: E402
from ergon.models import SearchQuery  # noqa: E402
from ergon.providers.eightfold import EightfoldProvider  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_ef_slugs.json"
_API = "https://api.tavily.com/search"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EXCLUDE = [
    "linkedin.com",
    "indeed.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "wikipedia.org",
    "levels.fyi",
    "builtin.com",
    "simplyhired.com",
    "monster.com",
    "dice.com",
    "myvisajobs.com",
    "h1bdata.info",
    "trackitt.com",
]

# Capture a tenant slug from any {slug}.eightfold.ai host; drop generic fronts.
_SLUG_RE = re.compile(r"(?:https?://)?([a-z0-9][a-z0-9-]*)\.eightfold\.ai", re.IGNORECASE)
_GENERIC = {"www", "app", "static", "assets", "cdn", "media", "img"}


def slug_from_urls(urls: list[str]) -> str | None:
    """First non-generic {slug}.eightfold.ai slug found across ``urls`` (pure; unit-tested)."""
    for u in urls:
        m = _SLUG_RE.search(u or "")
        if m:
            slug = m.group(1).lower()
            if slug not in _GENERIC and len(slug) >= 2:
                return slug
    return None


async def careers_urls(sponsor: str, key: str, fetcher: AsyncFetcher) -> list[str]:
    """Tavily-search for the sponsor's own careers page(s)."""
    body = {"query": f"{sponsor} careers jobs", "exclude_domains": EXCLUDE, "max_results": 4}
    try:
        data = await fetcher.post_json(_API, json=body, headers={"Authorization": f"Bearer {key}"})
    except Exception:  # noqa: BLE001
        return []
    results = data.get("results", []) if isinstance(data, dict) else []
    return [r.get("url", "") for r in results if r.get("url")]


async def capture_slug(urls: list[str], browser, limiter: anyio.CapacityLimiter) -> str | None:
    """Load each careers URL in Chromium; return the first {slug}.eightfold.ai it calls.

    A direct redirect to ``{slug}.eightfold.ai`` is caught too (the slug appears in a request
    URL regardless of whether it's an XHR or the top-level navigation).
    """
    for url in urls:
        if not url:
            continue
        async with limiter:
            reqs: list[str] = []
            try:
                ctx = await browser.new_context(user_agent=_UA)
                page = await ctx.new_page()
                page.on("request", lambda r, sink=reqs: sink.append(r.url))
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(4000)  # let the SPA fire its data XHRs
                except Exception:  # noqa: BLE001
                    pass
                reqs.append(page.url)  # final (possibly redirected) URL
                await ctx.close()
            except Exception:  # noqa: BLE001
                continue
        slug = slug_from_urls(reqs)
        if slug:
            return slug
    return None


async def main() -> None:
    args = sys.argv[1:]
    gap_file = ROOT / "runs" / "gap_eightfold.json"
    out_path = DEFAULT_OUT
    cap = 30
    i = 0
    while i < len(args):
        if args[i] == "--gap-file":
            gap_file = Path(args[i + 1])
            i += 2
        elif args[i] == "--out":
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
    sponsors = json.loads(gap_file.read_text())["uncovered_top"]
    # Skip sponsors already covered (any slug whose first word is already a seed key is a weak
    # check; the live verify gate is the real dedup — but skip obvious dupes to save browsers).
    todo = sponsors[:cap]
    print(f"slug-capture over {len(todo)} sponsors (cap {cap}) ...", flush=True)

    # Phase 1: Tavily careers-URL discovery (concurrent).
    urls_by_idx: dict[int, list[str]] = {}

    async def find(idx: int, sponsor: dict, fetcher: AsyncFetcher) -> None:
        urls_by_idx[idx] = await careers_urls(sponsor["name"], key, fetcher)

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=6, timeout=25.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, s in enumerate(todo):
            tg.start_soon(find, idx, s, fetcher)

    # Phase 2: headless-browser slug capture (bounded concurrency).
    captured: dict[int, str] = {}
    try:
        from playwright.async_api import async_playwright

        limiter = anyio.CapacityLimiter(6)
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            async def grab(idx: int) -> None:
                slug = await capture_slug(urls_by_idx.get(idx, []), browser, limiter)
                if slug:
                    captured[idx] = slug

            async with anyio.create_task_group() as tg:
                for idx in range(len(todo)):
                    tg.start_soon(grab, idx)
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  browser phase failed: {type(exc).__name__}: {str(exc)[:60]}")
        return

    print(f"captured {len(captured)} eightfold slugs; verifying live (PCSX) ...", flush=True)

    # Phase 3: verify each captured slug through the provider; keep live + not-already-seeded.
    provider = EightfoldProvider()
    candidates: list[dict] = []
    seen: set[str] = set()

    async def verify(idx: int, slug: str, fetcher: AsyncFetcher) -> None:
        if slug in seed_keys or slug in seen:
            return
        try:
            raws = await provider.fetch(slug, SearchQuery(limit=1), fetcher)
        except Exception:  # noqa: BLE001
            raws = []
        if raws:
            seen.add(slug)
            candidates.append(
                {
                    "company": slug,
                    "ats": "eightfold",
                    "token": slug,
                    "domain": None,
                    "_sponsor": todo[idx]["name"],
                }
            )

    async with (
        AsyncFetcher(concurrency=10, per_host_rate=8, timeout=25.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, slug in captured.items():
            tg.start_soon(verify, idx, slug, fetcher)

    for c in candidates:
        print(f"  + {c['_sponsor'][:34]:34} -> {c['token']}.eightfold.ai (live)")
    # Strip the provenance note before writing build_registry candidates.
    out = [{k: v for k, v in c.items() if k != "_sponsor"} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"\nwrote {len(out)} candidates -> {shown}")
    print(f"next: .venv/bin/python scripts/build_registry.py {shown}")


if __name__ == "__main__":
    anyio.run(main)
