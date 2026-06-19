"""Search-INTERACTION Playwright capture — the last lever for interaction-gated JS careers sites.

The remaining corps (DTCC, Two Sigma, Fujitsu, CGI, KPIT, Credit Karma, TikTok, GroupM, ...) fire
their job request only AFTER the search UI activates — a simple page-load capture sees nothing.
So per corp we load the job-search page, wait for network-idle, scroll, and click a search control,
then capture BOTH:

  * ATS-host requests — ``{tenant}.wdN.myworkdayjobs.com/wday/cxs``, ``*.successfactors.com``,
    ``*.avature.net``, ``*.icims.com``, ``*.oraclecloud.com``, ``/coveo/rest/search`` — which reveal
    the opaque tenant we couldn't guess; and
  * same-origin JSON responses carrying job RECORDS — an own-domain API (handled like apicapture).

Reports per corp what was found; ATS-host hits are turned into verified candidates via the real
providers. Discovery aid (manual review) — does NOT auto-merge own-API guesses.

Usage::

    .venv/bin/python scripts/capture_interactive.py
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

from capture_api import _find_records  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
OUT = ROOT / "scripts" / "candidates_interactive.json"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# (giant key, brand, job-SEARCH urls — prefer the page with the results list)
CORPS = [
    ("depositorytrustandclearing", "dtcc", ["https://careers.dtcc.com/search-jobs", "https://careers.dtcc.com/"]),
    ("twosigmainvestments", "two sigma", ["https://careers.twosigma.com/jobs", "https://careers.twosigma.com/"]),
    ("fujitsunorthamerica", "fujitsu", ["https://careers.fujitsu.com/en/search-jobs", "https://careers.fujitsu.com/en"]),
    ("creditkarma", "credit karma", ["https://www.creditkarma.com/careers/openings"]),
    ("tiktokusdatasecurity", "tiktok usds", ["https://careers.usds.tiktok.com/search", "https://careers.usds.tiktok.com/"]),
    ("cgitechnologiesandsolutions", "cgi", ["https://www.cgi.com/en/careers/search-careers"]),
    ("kpittechnologies", "kpit", ["https://www.kpit.com/careers/job-openings/"]),
    ("groupmworldwide", "groupm", ["https://www.groupm.com/careers/"]),
    ("valuemomentum", "valuemomentum", ["https://www.valuemomentum.com/careers/"]),
    ("landttechnologyservices", "ltts", ["https://www.ltts.com/careers"]),
]

_WD = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/([^/]+)/|(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+))", re.I)
_HOSTPATS = {
    "successfactors": re.compile(r"https?://([a-z0-9-]+)\.successfactors\.(?:com|eu)", re.I),
    "avature": re.compile(r"https?://([a-z0-9-]+\.avature\.net)", re.I),
    "icims": re.compile(r"https?://([a-z0-9-]+\.icims\.com)", re.I),
    "oracle": re.compile(r"https?://([a-z0-9-]+\.oraclecloud\.com)", re.I),
    "phenom": re.compile(r"https?://([a-z0-9-]+\.phenompeople\.com)", re.I),
    "coveo": re.compile(r"https?://([a-z0-9.-]+)/coveo/rest/search", re.I),
}


def _extract_host(url: str) -> tuple[str, str] | None:
    m = _WD.search(url)
    if m:
        site = m.group(3) or m.group(4)
        if site and site.lower() not in ("en-us", "wday"):
            return "workday", f"{m.group(1).lower()}|{m.group(2).lower()}|{site}"
    for ats, pat in _HOSTPATS.items():
        mm = pat.search(url)
        if mm:
            return ats, mm.group(1).lower()
    return None


async def _capture_one(ck, brand, urls, browser, limiter):
    hostsfound: list[tuple[str, str]] = []
    apifound: list[str] = []
    async with limiter:
        try:
            ctx = await browser.new_context(user_agent=_UA)
        except Exception:  # noqa: BLE001
            return ck, brand, hostsfound, apifound
        try:
            for u in urls:
                page = await ctx.new_page()

                def on_req(r: object) -> None:
                    hit = _extract_host(getattr(r, "url", ""))
                    if hit and hit not in hostsfound:
                        hostsfound.append(hit)

                async def on_resp(r: object) -> None:
                    try:
                        ct = (await r.header_value("content-type")) or ""  # type: ignore[attr-defined]
                        if "json" not in ct.lower():
                            return
                        data = await r.json()  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        return
                    rec = _find_records(data)
                    if rec and len(rec[1]) >= 3:
                        url = getattr(r, "url", "")
                        if url not in apifound:
                            apifound.append(url)

                page.on("request", on_req)
                page.on("response", on_resp)
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=12000)
                    except Exception:  # noqa: BLE001
                        pass
                    # nudge the SPA: scroll + try to click a search control
                    await page.mouse.wheel(0, 2000)
                    for sel in (
                        "button:has-text('Search')",
                        "button[type=submit]",
                        "[class*=search] button",
                        "input[type=submit]",
                    ):
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.click(timeout=2500)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    await page.wait_for_timeout(6000)
                except Exception:  # noqa: BLE001
                    pass
                await page.close()
                if hostsfound:
                    break
        finally:
            await ctx.close()
    return ck, brand, hostsfound, apifound


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(4)

        async def go(ck, brand, urls):
            res = await _capture_one(ck, brand, urls, browser, limiter)
            results.append(res)
            ck, brand, hosts, apis = res
            print(f"  {ck:28} hosts={hosts} api={[a[:50] for a in apis[:2]]}", flush=True)

        async with anyio.create_task_group() as tg:
            for ck, brand, urls in CORPS:
                tg.start_soon(go, ck, brand, urls)
        await browser.close()

    # Verify ATS-host hits through the real providers.
    candidates = []
    async with AsyncFetcher(concurrency=6, per_host_rate=4, timeout=25.0, retries=1) as f:
        for ck, brand, hosts, _apis in results:
            for ats, token in hosts:
                try:
                    raws = await get_provider(ats).fetch(token, SearchQuery(limit=5), f)
                except Exception:  # noqa: BLE001
                    raws = []
                if raws:
                    print(f"  verify {ck:26} {ats:9} {token[:34]:34} {len(raws)}j co={raws[0].company[:18]}")
                    c = {"company": ck, "ats": ats, "domain": None}
                    if ats == "workday":
                        t, w, s = token.split("|", 2)
                        c.update({"tenant": t, "wd": w, "site": s})
                    else:
                        c["token"] = token
                    candidates.append(c)
                    break

    OUT.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(candidates)} candidates -> {OUT.name}")


if __name__ == "__main__":
    anyio.run(main)
