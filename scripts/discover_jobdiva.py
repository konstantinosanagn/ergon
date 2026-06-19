"""One-time DISCOVERY of JobDiva candidate-portal hashes for residual staffing/IT giants.

Many residual H-1B mega-sponsors are IT-staffing firms with no own ATS — their careers pages
embed a JobDiva portal (``www1.jobdiva.com/portal/?a={hash}``). The portal is a JS SPA, but its
data comes from a plain JSON API we already replicate at runtime in the ``jobdiva`` provider
(NO browser at fetch time). This script does the *one-time* discovery: load each firm's careers
page in a headless browser, capture the ``a={hash}`` portal id from any jobdiva.com request, then
VERIFY by fetching live jobs through the provider. Emits build_registry candidates.

    .venv/bin/python scripts/discover_jobdiva.py [--cap N]
    .venv/bin/python scripts/build_registry.py scripts/candidates_jobdiva.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import anyio  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from census_successfactors import tavily  # noqa: E402  # isort: skip

GIANTS = ROOT / "runs" / "giants.json"
OUT = ROOT / "scripts" / "candidates_jobdiva.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HASH_RE = re.compile(r"[?&]a=([A-Za-z0-9]{24,})")
# Residual firms that look like staffing / IT-services shops (the JobDiva-heavy segment).
_STAFFING_HINT = re.compile(
    r"\b(consult|consulting|staffing|solution|solutions|technolog|technologies|software|"
    r"systems|infotech|services|labs|global|group|it|inc|llc|resourc|talent|infosys)\b",
    re.I,
)
# Known/likely JobDiva-cluster residual GIANTS (IT-staffing shops), highest-value first.
_SEED_NAMES = [
    "compunnel software group",
    "grandison management",
    "skilltune technologies",
    "infocons",
    "intellectt",
    "natsoft",
    "slk america",
    "gp technologies",
    "populus group",
    "diaspark",
    "tek leaders",
    "astir it solutions",
    "avco consulting",
    "satin solutions",
    "quadrant technologies",
    "orpine",
    "intellisoft systems",
]


def _brand(name: str) -> str:
    n = re.sub(r"\b(inc|llc|corp|corporation|ltd|limited|co|company|usa|us|lp|l p|na|n a)\b", "", name, flags=re.I)
    return re.sub(r"\s+", " ", n).strip()


def _candidate_names(cap: int) -> list[str]:
    names = list(_SEED_NAMES)
    try:
        giants = json.load(open(GIANTS))["uncovered_top"]
    except Exception:
        giants = []
    for g in giants:
        nm = g.get("name", "")
        if _STAFFING_HINT.search(nm) and nm not in names:
            names.append(nm)
    return names[:cap]


async def _careers_urls(name: str, key: str, fetcher) -> list[str]:
    brand = _brand(name)
    urls: list[str] = []
    # Target the firm's OWN site (no "jobdiva" term — that pollutes with random staffing portals).
    for q in (f"{brand} careers", f"{brand} current openings jobs"):
        try:
            hits = await tavily(q, key, fetcher)
        except Exception:
            hits = []
        for u in hits:
            if u and u not in urls and "jobdiva.com/career" not in u:
                urls.append(u)
    # direct jobdiva portal guess (some firms expose it indexed)
    return urls[:5]


async def _grab_hash(name: str, urls: list[str], browser) -> str | None:
    """Load careers pages; return the first jobdiva portal hash seen in any request URL."""
    from playwright.async_api import TimeoutError as PWTimeout

    for u in urls[:3]:
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()
        found: list[str] = []

        def on_req(r):
            if "jobdiva.com" in r.url:
                m = _HASH_RE.search(r.url)
                if m:
                    found.append(m.group(1))

        page.on("request", on_req)
        try:
            await page.goto(u, wait_until="domcontentloaded", timeout=18000)
            await page.wait_for_timeout(4000)
            # also scan rendered HTML for an embedded portal hash
            html = await page.content()
            m = _HASH_RE.search(html)
            if m:
                found.append(m.group(1))
        except (PWTimeout, Exception):
            pass
        finally:
            await ctx.close()
        if found:
            return found[0]
    return None


async def _brandname(h: str, teamid: str, fetcher) -> str | None:
    """The authoritative portal owner from JobDiva's own ``team`` record."""
    try:
        auth = await fetcher.get_json(
            "https://ws.jobdiva.com/candPortal/rest/auth/a",
            params={"a": h},
            headers={"portalid": "1", "compid": "-1", "a": h},
        )
        tok = auth.get("token")
        resp = await fetcher.request(
            "GET",
            "https://ws.jobdiva.com/candPortal/rest/team",
            headers={"portalid": teamid, "compid": "0", "token": tok},
        )
        if resp.status_code == 200:
            bn = resp.json().get("brandname")
            return bn.strip() if isinstance(bn, str) and bn.strip() else None
    except Exception:
        return None
    return None


async def _verify(h: str, fetcher) -> tuple[int, str | None, str | None]:
    """Return (job_count, teamid, brandname) for a portal hash via the live provider."""
    prov = get_provider("jobdiva")
    teamid = await prov._teamid(h, fetcher)  # type: ignore[attr-defined]
    if not teamid:
        return 0, None, None
    bn = await _brandname(h, teamid, fetcher)
    full = await prov.fetch(f"{h}|{teamid}|probe", SearchQuery(), fetcher)
    return len(full), teamid, bn


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=40)
    args = ap.parse_args()
    load_builtins()
    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set")
        return
    names = _candidate_names(args.cap)
    print(f"discovering JobDiva portals for {len(names)} residual firms...")

    # 1) find careers URLs (concurrent Tavily)
    name_urls: dict[str, list[str]] = {}
    async with AsyncFetcher() as fetcher:
        async with anyio.create_task_group() as tg:
            async def _f(nm):
                name_urls[nm] = await _careers_urls(nm, key, fetcher)
            for nm in names:
                tg.start_soon(_f, nm)

    # 2) Playwright capture hashes (bounded concurrency)
    from playwright.async_api import async_playwright

    hashes: dict[str, str] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        sem = asyncio.Semaphore(4)

        async def _g(nm):
            async with sem:
                u = name_urls.get(nm) or []
                if not u:
                    return
                h = await _grab_hash(nm, u, browser)
                if h:
                    hashes[nm] = h
                    print(f"  HASH {nm:32s} {h[:20]}...")

        await asyncio.gather(*[_g(nm) for nm in names])
        await browser.close()

    # 3) verify + identify by authoritative brandname, then match to residual giants
    from harvest_tokens import _core, name_match  # noqa: E402

    seed = json.load(open(ROOT / "src/ergon_tracker/registry/data/seed.json"))["companies"]
    giants = [g["name"] for g in json.load(open(GIANTS)).get("uncovered_top", [])]

    def _match_giant(bn: str) -> str | None:
        bc = _core(bn)
        for g in giants:
            if _core(g) == bc or name_match(g, bn):
                return g
        return None

    candidates = []
    seen_hash: set[str] = set()
    async with AsyncFetcher() as fetcher:
        for nm, h in hashes.items():
            if h in seen_hash:
                continue
            seen_hash.add(h)
            try:
                cnt, teamid, bn = await _verify(h, fetcher)
            except Exception as e:
                print(f"  verify ERR {nm}: {type(e).__name__}")
                continue
            if cnt < 1 or not teamid or not bn:
                print(f"  SKIP   searched={nm:30s} teamid={teamid} jobs={cnt} brand={bn}")
                continue
            giant = _match_giant(bn)
            key = _core(bn)
            already = key in seed
            tag = (
                f"GIANT={giant}" if giant else ("DUP-in-seed" if already else "clean-new")
            )
            print(f"  OK searched={nm:28s} brand={bn:30s} teamid={teamid} jobs={cnt:4d} -> {tag}")
            # Seed authoritative-brandname portals: matched giants (relabeled), or clean-new firms.
            if already and not giant:
                continue  # don't clobber an existing seed entry
            candidates.append(
                {
                    "company": _core(giant) if giant else key,
                    "ats": "jobdiva",
                    "token": f"{h}|{teamid}|{bn}",
                    "domain": None,
                }
            )

    OUT.write_text(json.dumps(candidates, indent=2))
    print(f"\nwrote {len(candidates)} JobDiva candidates -> {OUT}")


if __name__ == "__main__":
    anyio.run(main)
