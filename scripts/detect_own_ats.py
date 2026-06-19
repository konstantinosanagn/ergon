"""Detect the AUTHORITATIVE own-ATS for giants currently stuck on the weak ``adzuna`` fallback,
so ``upgrade_registry.py`` can replace the aggregator entry with the real board.

Per giant: Tavily "{brand} careers" -> keep only careers URLs on a BRAND-RELATED domain (kills
third-party aggregator/embed noise) -> headless-load each -> capture ATS host requests -> extract
the precise token per ATS family -> LIVE-verify the token returns jobs. Emits upgrade candidates.

    .venv/bin/python scripts/detect_own_ats.py [--cap N] [--out scripts/candidates_upgrade.json]
    .venv/bin/python scripts/upgrade_registry.py scripts/candidates_upgrade.json

Entity safety: opaque-tenant ATSes (Workday ``ghr``, Oracle ``egug``) aren't brand-derivable, so
we trust them ONLY when found on a careers page whose host relates to the brand (the giant's own
site embeds its own board). ATS on a non-brand host is rejected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import anyio  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from build_registry import ATS_PRIORITY  # noqa: E402
from capture_api import _brand_tokens, _host_relates  # noqa: E402
from census_residual import brand_query  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core  # noqa: E402
from census_successfactors import tavily  # noqa: E402  # isort: skip

SEED = ROOT / "src/ergon_tracker/registry/data/seed.json"
GIANTS = ROOT / "runs/giants.json"
DEFAULT_OUT = ROOT / "scripts/candidates_upgrade.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _adzuna_giants() -> list[tuple[str, str]]:
    """(_core key, giant display name) for giants currently on the adzuna fallback, by filings."""
    seed = json.loads(SEED.read_text())["companies"]
    giants = json.loads(GIANTS.read_text())["uncovered_top"]
    out = []
    for g in giants:
        k = _core(g["name"])
        e = seed.get(k)
        if e and e.get("ats") == "adzuna":
            out.append((g.get("filings") or 0, k, g["name"]))
    out.sort(reverse=True)
    return [(k, n) for _f, k, n in out]


# --- token extraction per ATS family (returns (ats, token) or None) ---------------------------
def _extract(urls: list[str], page_host: str) -> tuple[str, str] | None:
    blob = "\n".join(urls)
    # Workday: /wday/cxs/{tenant}/{site}/jobs  ->  {tenant}|wd{N}|{site}
    m = re.search(r"//([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/[a-z0-9-]+/([A-Za-z0-9_-]+)/", blob, re.I)
    if m:
        return "workday", f"{m.group(1)}|{m.group(2)}|{m.group(3)}"
    # Oracle Recruiting Cloud: host + CX_NNNN
    mh = re.search(r"//([a-z0-9-]+\.fa\.[a-z0-9]+\.oraclecloud\.com)", blob, re.I)
    mc = re.search(r"(?:siteNumber=|/sites/)(CX_\d+)", blob)
    if mh and mc:
        return "oracle", f"{mh.group(1)}|{mc.group(1)}"
    # Phenom: token = the careers page host (the /widgets + /job pages live there)
    if "phenompeople.com" in blob or "phenom.com" in blob:
        if page_host:
            return "phenom", page_host
    # PeopleAdmin: {sub}.peopleadmin.com
    m = re.search(r"//([a-z0-9-]+)\.peopleadmin\.com", blob, re.I)
    if m:
        return "peopleadmin", m.group(1)
    # iCIMS: {tenant}.icims.com (strip the careers-/activeposting- service prefixes)
    m = re.search(r"//(?:careers-|activeposting-|jobs-)?([a-z0-9-]+)\.icims\.com", blob, re.I)
    if m and m.group(1) not in {"www", "cdn", "cdn02", "cookie-policy-scripts", "images", "static"}:
        return "icims", m.group(1)
    # Taleo: {tenant}.taleo.net (bare host -> provider auto-discovers cs/portal)
    m = re.search(r"//([a-z0-9-]+)\.taleo\.net", blob, re.I)
    if m and m.group(1) not in {"www", "staticphf"}:
        return "taleo", f"{m.group(1)}.taleo.net"
    # Greenhouse / Lever / Ashby slugs
    for ats, pat in (("greenhouse", r"greenhouse\.io/([a-z0-9]+)"), ("lever", r"jobs\.lever\.co/([a-z0-9-]+)"), ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9-]+)")):
        m = re.search(pat, blob, re.I)
        if m:
            return ats, m.group(1)
    return None


_ATS_REQ = ("myworkdayjobs.com", "oraclecloud.com", "phenompeople.com", "phenom.com",
            "peopleadmin.com", "icims.com", "taleo.net", "greenhouse.io", "lever.co", "ashbyhq.com")


async def _careers_urls(name: str, key: str, fetcher) -> list[str]:
    brand = brand_query(name) or name
    urls: list[str] = []
    try:
        hits = await tavily(f"{brand} careers jobs", key, fetcher)
    except Exception:
        hits = []
    for u in hits:
        if not u:
            continue
        host = urlsplit(u).netloc.lower()
        # keep only the brand's OWN domain (kills aggregator/third-party careers pages)
        if _host_relates(host, name) or _host_relates(host, brand):
            urls.append(u)
    return urls[:4]


async def _capture(urls: list[str], browser) -> tuple[str, str] | None:
    from playwright.async_api import async_playwright  # noqa: F401

    for u in urls:
        ctx = await browser.new_context(user_agent=_UA)
        pg = await ctx.new_page()
        reqs: list[str] = []
        pg.on("request", lambda r: reqs.append(r.url) if any(h in r.url.lower() for h in _ATS_REQ) else None)
        try:
            await pg.goto(u, wait_until="networkidle", timeout=25000)
            await pg.wait_for_timeout(4000)
        except Exception:
            pass
        page_host = urlsplit(u).netloc.lower()
        await ctx.close()
        hit = _extract(reqs, page_host)
        if hit:
            return hit
    return None


async def _verify(ats: str, token: str, fetcher) -> int:
    try:
        raws = await get_provider(ats).fetch(token, SearchQuery(limit=60), fetcher)
        return len(raws)
    except Exception:
        return 0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=30)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    load_builtins()
    key = load_key()
    giants = _adzuna_giants()[: args.cap]
    print(f"detecting own-ATS for {len(giants)} adzuna giants...", flush=True)

    # 1) careers URLs (concurrent Tavily)
    urls_by: dict[str, list[str]] = {}
    async with AsyncFetcher() as fetcher:
        async with anyio.create_task_group() as tg:
            async def _f(k, n):
                urls_by[k] = await _careers_urls(n, k, fetcher)
            for k, n in giants:
                tg.start_soon(_f, k, n)

    # 2) Playwright capture + extract token (bounded)
    from playwright.async_api import async_playwright

    found: dict[str, tuple[str, str]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        sem = asyncio.Semaphore(4)

        async def _g(k, n):
            async with sem:
                u = urls_by.get(k) or []
                if not u:
                    return
                hit = await _capture(u, browser)
                if hit:
                    found[k] = hit
                    print(f"  DETECT {k:30s} {hit[0]:13s} {hit[1][:46]}", flush=True)

        await asyncio.gather(*[_g(k, n) for k, n in giants])
        await browser.close()

    # 3) live-verify + emit upgrade candidates (only if it outranks adzuna)
    seed = json.loads(SEED.read_text())["companies"]
    name_by = dict(giants)
    cands = []
    async with AsyncFetcher() as fetcher:
        for k, (ats, token) in found.items():
            cnt = await _verify(ats, token, fetcher)
            better = ATS_PRIORITY.get(ats, 99) < ATS_PRIORITY.get(seed.get(k, {}).get("ats"), 99)
            tag = "OK" if (cnt >= 1 and better) else ("0-jobs" if cnt < 1 else "not-better")
            print(f"  VERIFY {k:30s} {ats:13s} jobs={cnt:4d} -> {tag}", flush=True)
            if cnt >= 1 and better:
                cands.append({"company": k, "ats": ats, "token": token, "domain": None})

    Path(args.out).write_text(json.dumps(cands, indent=2))
    print(f"\nwrote {len(cands)} upgrade candidates -> {args.out}")


if __name__ == "__main__":
    anyio.run(main)
