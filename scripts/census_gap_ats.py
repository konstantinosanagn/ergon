"""3-layer census: which ATS each uncovered H-1B sponsor uses -> ranked build-priority report.

Detecting a company's ATS is hard because big employers hide it. We layer three methods per
sponsor, cheapest/most-reliable first, falling through on "unknown":

  L1  host-restricted ATS search  — Tavily, results pinned to shared ATS hosts (Workday/iCIMS/
      Taleo/Greenhouse/...). Catches giants whose underlying board is publicly indexed even when
      their careers page proxies it (Walmart -> walmart.wd5.myworkdayjobs.com).
  L2  static careers-page scan    — fetch the careers page's RAW HTML and scan for ATS signatures
      in href/src/script tags. Catches companies that link/embed their ATS.
  L3  headless browser capture    — load the careers SPA in Chromium and watch its network
      requests; the ATS API host appears even when it's nowhere in the static HTML
      (Microsoft -> eightfold.ai). Run only on what L1/L2 couldn't resolve.

What remains after L3 is "proxied/opaque" — mega-giants (Walmart, Apple, Google) that call their
ATS server-to-server, so it's invisible to any client method. The output aggregates by ATS,
weighted by each sponsor's filing volume: "build a provider for X -> unlock N filings".

Usage::

    .venv/bin/python scripts/census_gap_ats.py [--top 1000] [--browser-cap 400] [--out PATH]
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_tavily import load_key  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402

GAP = ROOT / "runs" / "h1b_coverage_gap.json"
DEFAULT_OUT = ROOT / "runs" / "gap_ats_census.json"
_API = "https://api.tavily.com/search"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
EXCLUDE = ["linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com", "wikipedia.org",
           "levels.fyi", "builtin.com", "simplyhired.com", "monster.com", "dice.com"]

# (signature substring, ats, supported?). Supported entries first -> preferred on ties.
SIGNATURES: list[tuple[str, str, bool]] = [
    ("boards.greenhouse.io", "greenhouse", True), ("grnh.se", "greenhouse", True),
    ("jobs.lever.co", "lever", True), ("jobs.ashbyhq.com", "ashby", True),
    ("myworkdayjobs.com", "workday", True), ("workdayjobs.com", "workday", True),
    ("apply.workable.com", "workable", True), ("smartrecruiters.com", "smartrecruiters", True),
    ("ats.rippling.com", "rippling", True), ("pinpointhq.com", "pinpoint", True),
    ("bamboohr.com", "bamboohr", True), ("breezy.hr", "breezy", True),
    ("teamtailor.com", "teamtailor", True), ("recruitee.com", "recruitee", True),
    ("icims.com", "icims", False), ("taleo.net", "taleo", False),
    ("successfactors", "successfactors", False), ("sapsf", "successfactors", False),
    ("eightfold.ai", "eightfold", False), ("jobvite.com", "jobvite", False),
    ("phenompeople", "phenom", False), ("avature.net", "avature", False),
    ("applytojob.com", "jazzhr", False), ("oraclecloud.com", "oracle", False),
    ("/hcmui/", "oracle", False), ("dayforcehcm", "dayforce", False),
    ("ultipro", "ukg", False), ("paylocity", "paylocity", False),
    ("workforcenow", "adp", False), ("brassring", "brassring", False),
]
# Shared-tenant ATS hosts to pin L1 search to (the board lives on the vendor host).
SEARCH_HOSTS = ["boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com",
                "smartrecruiters.com", "myworkdayjobs.com", "ats.rippling.com", "icims.com",
                "taleo.net", "successfactors.com", "jobvite.com", "eightfold.ai", "avature.net",
                "applytojob.com", "oraclecloud.com"]
SUPPORTED = {a for _, a, s in SIGNATURES if s}


def detect_ats(text: str) -> tuple[str, bool] | None:
    low = text.lower()
    for sig, name, supported in SIGNATURES:
        if sig in low:
            return name, supported
    return None


def _collapse(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def name_in_url(sponsor: str, url: str) -> bool:
    """Loose adjudication: a significant word of the sponsor appears in the (collapsed) URL."""
    u = _collapse(url)
    words = [_collapse(w) for w in re.sub(r"[^a-z0-9 ]", " ", sponsor.lower()).split()]
    return any(len(w) >= 4 and w in u for w in words) or _collapse(sponsor)[:10] in u


# --- layers 1 & 2 (Tavily + raw-HTML; concurrent) ---------------------------------------------


async def layer1_host_search(sponsor: str, key: str, fetcher: AsyncFetcher) -> str | None:
    """Search the shared ATS hosts directly; return the ats if a name-matching board is found."""
    body = {"query": f"{sponsor} careers jobs", "include_domains": SEARCH_HOSTS, "max_results": 6}
    try:
        data = await fetcher.post_json(_API, json=body, headers={"Authorization": f"Bearer {key}"})
    except Exception:  # noqa: BLE001
        return None
    for x in (data.get("results", []) if isinstance(data, dict) else []):
        url = x.get("url", "")
        hit = detect_ats(url)
        if hit and name_in_url(sponsor, url):
            return hit[0]
    return None


async def layer2_static(sponsor: str, key: str, fetcher: AsyncFetcher) -> tuple[str | None, str]:
    """Find the careers page and scan its RESULT URLs for an ATS signature. Returns
    (ats|None, careers_url). No slow HTML fetch — L3's headless browser does the deep read,
    and careers pages are routinely slow/anti-bot, so fetching them here just stalls the sweep.
    """
    body = {"query": f"{sponsor} careers jobs", "exclude_domains": EXCLUDE, "max_results": 3}
    try:
        data = await fetcher.post_json(_API, json=body, headers={"Authorization": f"Bearer {key}"})
    except Exception:  # noqa: BLE001
        return None, ""
    urls = [x.get("url", "") for x in (data.get("results", []) if isinstance(data, dict) else [])]
    for u in urls:
        hit = detect_ats(u)
        if hit:
            return hit[0], (urls[0] if urls else "")
    return None, (urls[0] if urls else "")


# --- layer 3 (headless browser network capture) -----------------------------------------------


async def layer3_browser(url: str, browser, limiter: anyio.CapacityLimiter) -> str | None:
    """Load the careers page in Chromium and detect the ATS from its network requests."""
    if not url:
        return None
    async with limiter:
        try:
            ctx = await browser.new_context(user_agent=_UA["User-Agent"])
            page = await ctx.new_page()
            reqs: list[str] = []
            page.on("request", lambda r: reqs.append(r.url))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)  # let the SPA fire its data XHR
            except Exception:  # noqa: BLE001
                pass
            await ctx.close()
        except Exception:  # noqa: BLE001
            return None
        for u in reqs:
            hit = detect_ats(u)
            if hit:
                return hit[0]
    return None


# --- orchestration ----------------------------------------------------------------------------


async def main() -> None:
    args = sys.argv[1:]
    top, browser_cap, out_path = 1000, 400, DEFAULT_OUT
    i = 0
    while i < len(args):
        if args[i] == "--top":
            top = int(args[i + 1])
            i += 2
        elif args[i] == "--browser-cap":
            browser_cap = int(args[i + 1])
            i += 2
        elif args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set.")
        return
    sponsors = json.loads(GAP.read_text())["uncovered_top"][:top]
    print(f"L1+L2 (concurrent) over {len(sponsors)} sponsors ...", flush=True)

    # Phase A: layers 1 & 2 concurrently.
    resolved: dict[int, str] = {}
    careers: dict[int, str] = {}
    done = [0]

    async def phase_a(idx: int, sponsor: dict, fetcher: AsyncFetcher) -> None:
        name = sponsor["name"]
        ats = await layer1_host_search(name, key, fetcher)
        if ats is None:
            ats, careers[idx] = await layer2_static(name, key, fetcher)
        if ats is not None:
            resolved[idx] = ats
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  L1+L2 {done[0]}/{len(sponsors)} ...", flush=True)

    async with (
        AsyncFetcher(concurrency=16, per_host_rate=8, timeout=20.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, s in enumerate(sponsors):
            tg.start_soon(phase_a, idx, s, fetcher)

    # Phase B: headless browser on the still-unknown sponsors (top by filings, capped).
    unknown = sorted(
        ((idx, careers.get(idx, "")) for idx in range(len(sponsors)) if idx not in resolved),
        key=lambda t: -sponsors[t[0]]["filings"],
    )[:browser_cap]
    print(f"L3 (headless browser) over {len(unknown)} unresolved sponsors ...", flush=True)
    try:
        from playwright.async_api import async_playwright

        limiter = anyio.CapacityLimiter(6)
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            async def phase_b(idx: int, url: str) -> None:
                ats = await layer3_browser(url, browser, limiter)
                resolved[idx] = ats if ats else "proxied/opaque"

            async with anyio.create_task_group() as tg:
                for idx, url in unknown:
                    tg.start_soon(phase_b, idx, url)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - playwright optional / may fail
        print(f"  L3 skipped: {type(exc).__name__}: {str(exc)[:60]}")

    # Aggregate by ATS, weighted by filings.
    per_sponsor: list[dict] = []
    n_sponsors: dict[str, int] = defaultdict(int)
    n_filings: dict[str, int] = defaultdict(int)
    for idx, s in enumerate(sponsors):
        ats = resolved.get(idx, "js-spa/unknown")
        per_sponsor.append({"name": s["name"], "filings": s["filings"], "ats": ats})
        n_sponsors[ats] += 1
        n_filings[ats] += s["filings"]

    ranked = sorted(n_filings.items(), key=lambda kv: -kv[1])
    report = {
        "scope": f"top {len(sponsors)} uncovered sponsors by filing volume",
        "by_ats": [{"ats": a, "supported": a in SUPPORTED, "sponsors": n_sponsors[a],
                    "filings": n_filings[a]} for a, _ in ranked],
        "per_sponsor": per_sponsor,
    }
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    print(f"\n=== BUILD-PRIORITY REPORT (top {len(sponsors)}, by filings) ===")
    print(f"  {'ATS':18}{'verdict':>9}{'sponsors':>10}{'filings':>10}")
    opaque = ("proxied/opaque", "js-spa/unknown", "search-error")
    for a, f in ranked:
        tag = "have" if a in SUPPORTED else ("—" if a in opaque else "BUILD")
        print(f"  {a:18}{tag:>9}{n_sponsors[a]:>10}{f:>10}")
    buildable = sum(f for a, f in ranked if a not in SUPPORTED and a not in opaque)
    print(f"\nfilings unlockable by building unsupported providers: {buildable:,}")
    try:
        print(f"wrote {out_path.relative_to(ROOT)}")
    except ValueError:
        print(f"wrote {out_path}")


if __name__ == "__main__":
    anyio.run(main)
