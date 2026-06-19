"""Find the REAL job API behind a JS-gated careers page (generalises the UCLA discovery).

Many residual giants' public careers host is a JS stub that, once rendered, calls the real board
API on a different host — UCLA's ``careers-ucla.icims.com`` (138-byte stub) loads
``jobs.ucla.edu/api/jobs`` (iCIMS Jibe). We render each giant's careers page, watch every JSON
response for one carrying job RECORDS, and report (host, url, sample-count) classified to a known
provider (Jibe ``/api/jobs`` -> icims, ``myworkdayjobs`` -> workday, ``/coveo/rest`` -> coveo,
successfactors, avature, ...) or 'own-api' (candidate for apicapture). Manual-review aid — does
NOT auto-merge.

Usage::

    .venv/bin/python scripts/find_job_api.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from capture_api import _find_records  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

from census_successfactors import tavily  # noqa: E402  # isort: skip

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# label -> Tavily query to discover the real careers/job-search URL (proven URLs beat guesses).
QUERIES = {
    "pitt": "University of Pittsburgh staff careers job search",
    "mit": "MIT careers job openings search",
    "rutgers": "Rutgers University jobs careers search",
    "michiganstate": "Michigan State University careers job search",
    "missouri": "University of Missouri jobs careers search",
    "westvirginia": "West Virginia University careers job search",
    "floridastate": "Florida State University jobs careers search",
    "arizonastate": "Arizona State University staff careers job search",
    "fiu": "Florida International University careers job search",
    "dartmouth": "Dartmouth College careers job search",
    "cincinnati": "University of Cincinnati careers job search",
    "newmexico": "University of New Mexico jobs careers search",
    "dtcc": "DTCC careers search jobs",
    "fujitsu": "Fujitsu careers job search apply",
    "cgi": "CGI careers search jobs",
    "kpit": "KPIT careers job openings",
    "creditkarma": "Credit Karma careers job openings",
    "tiktokusds": "TikTok USDS careers job search",
    "groupm": "GroupM careers job search",
    "ust": "UST careers job search apply",
    "mathworks": "MathWorks careers job opportunities search",
}

# (label, careers URLs to try in order)  -- fallback if Tavily yields nothing
TARGETS = [
    ("pitt", ["https://careers.pitt.edu/", "https://cfo.pitt.edu/ohr/careers/"]),
    ("mit", ["https://careers.mit.edu/", "https://hr.mit.edu/careers"]),
    ("rutgers", ["https://jobs.rutgers.edu/", "https://uhr.rutgers.edu/careers"]),
    ("michiganstate", ["https://careers.msu.edu/"]),
    ("missouri", ["https://hrs.missouri.edu/find-a-job", "https://jobs.missouri.edu/"]),
    ("westvirginia", ["https://careers.wvu.edu/", "https://jobs.wvu.edu/"]),
    ("floridastate", ["https://jobs.fsu.edu/", "https://hr.fsu.edu/careers"]),
    ("arizonastate", ["https://careers.asu.edu/", "https://sjobs.asu.edu/"]),
    ("fiu", ["https://careers.fiu.edu/", "https://hr.fiu.edu/careers/"]),
    ("dartmouth", ["https://careers.dartmouth.edu/", "https://jobs.dartmouth.edu/"]),
    ("cincinnati", ["https://jobs.uc.edu/", "https://careers.uc.edu/"]),
    ("newmexico", ["https://jobs.unm.edu/", "https://careers.unm.edu/"]),
    ("dtcc", ["https://careers.dtcc.com/", "https://www.dtcc.com/careers/search-careers"]),
    (
        "fujitsu",
        ["https://careers.fujitsu.com/en", "https://www.fujitsu.com/global/about/careers/"],
    ),
    ("cgi", ["https://www.cgi.com/en/careers/search-careers"]),
    ("kpit", ["https://www.kpit.com/careers/job-openings/"]),
    ("creditkarma", ["https://www.creditkarma.com/careers/openings"]),
    ("tiktokusds", ["https://careers.usds.tiktok.com/"]),
    ("groupm", ["https://www.groupm.com/careers/"]),
    ("ust", ["https://www.ust.com/en/careers", "https://careers.ust.com/"]),
    ("mathworks", ["https://www.mathworks.com/company/jobs/opportunities/"]),
]

_KNOWN = [
    (re.compile(r"myworkdayjobs\.com|/wday/cxs/", re.I), "workday"),
    (re.compile(r"\.icims\.com|/api/jobs", re.I), "icims/jibe"),
    (re.compile(r"/coveo/rest|coveo\.com", re.I), "coveo"),
    (re.compile(r"successfactors", re.I), "successfactors"),
    (re.compile(r"\.avature\.net", re.I), "avature"),
    (re.compile(r"\.oraclecloud\.com", re.I), "oracle"),
    (re.compile(r"phenompeople|/widgets", re.I), "phenom"),
    (re.compile(r"greenhouse\.io", re.I), "greenhouse"),
    (re.compile(r"lever\.co", re.I), "lever"),
    (re.compile(r"peopleadmin", re.I), "peopleadmin"),
    (re.compile(r"smartrecruiters", re.I), "smartrecruiters"),
]
_DENY = re.compile(r"paradox|bebee|jobdiva|chronicle|consentmo|indeed|linkedin|glassdoor", re.I)


def classify(url: str) -> str:
    for pat, name in _KNOWN:
        if pat.search(url):
            return name
    return "own-api"


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return

    # Discover real careers URLs via Tavily (proven URLs beat guesses); fall back to hardcoded.
    from ergon_tracker.http import AsyncFetcher

    fallback = dict(TARGETS)
    urls_by_label: dict[str, list[str]] = {}
    key = load_key()
    if key:
        async with (
            AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=2) as tav,
            anyio.create_task_group() as tg,
        ):

            async def resolve(label: str, q: str) -> None:
                try:
                    found = await tavily(q, key, tav)
                except Exception:  # noqa: BLE001
                    found = []
                urls = [u for u in found if u.startswith("http")][:3]
                urls_by_label[label] = urls + fallback.get(label, [])

            for label, q in QUERIES.items():
                tg.start_soon(resolve, label, q)
    targets = [(lbl, urls_by_label.get(lbl) or fallback.get(lbl, [])) for lbl in QUERIES]

    results: dict[str, list[tuple[str, int, str]]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(4)

        async def scan(label: str, urls: list[str]) -> None:
            hits: list[tuple[str, int, str]] = []
            async with limiter:
                ctx = await browser.new_context(user_agent=_UA)
                try:
                    for u in urls:
                        page = await ctx.new_page()

                        async def on_resp(r: object) -> None:
                            url = getattr(r, "url", "")
                            if _DENY.search(url):
                                return
                            try:
                                ct = (await r.header_value("content-type")) or ""  # type: ignore[attr-defined]
                                if "json" not in ct.lower():
                                    return
                                data = await r.json()  # type: ignore[attr-defined]
                            except Exception:  # noqa: BLE001
                                return
                            rec = _find_records(data)
                            host = urlsplit(url).netloc
                            if rec and len(rec[1]) >= 3:
                                tup = (host, len(rec[1]), classify(url))
                                if tup not in hits:
                                    hits.append(tup)

                        page.on("response", on_resp)
                        try:
                            await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:  # noqa: BLE001
                                pass
                            await page.wait_for_timeout(5000)
                        except Exception:  # noqa: BLE001
                            pass
                        await page.close()
                        if hits:
                            break
                finally:
                    await ctx.close()
            results[label] = hits
            tag = hits[0] if hits else None
            print(f"  {label:16} -> {tag if tag else 'no job API found'}", flush=True)

        async with anyio.create_task_group() as tg:
            for label, urls in targets:
                tg.start_soon(scan, label, urls)
        await browser.close()

    print("\n=== summary ===")
    for label, hits in results.items():
        for host, n, prov in hits:
            print(f"  {label:16} {prov:14} {host}  (~{n} records/page)")


if __name__ == "__main__":
    anyio.run(main)
