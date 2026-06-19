"""Capture the ATS API call a JS-rendered careers page makes — to recover OPAQUE ATS tenants.

The remaining big residual corps (Two Sigma, DTCC, Fujitsu, SLB, CGI, Skyworks, Slalom, ...) have
fully JS-rendered careers pages: no ATS host appears in the static HTML, and their Workday/Avature
tenant is an opaque code we can't guess or relate. BUT when the SPA loads, the browser itself
fires the job request straight at the real ATS host —
``{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`` (POST), or
``{host}.avature.net/...``, ``{host}.icims.com/...``, ``...oraclecloud.com...``. Capturing that
network request reveals the tenant + site AUTHORITATIVELY (it's the company's own page calling its
own board). We then verify through the existing provider + adjudicate, and emit a candidate.

Unlike capture_api (own-domain JSON records), this watches for ATS-host REQUESTS (cross-domain is
expected — the ATS host is a different domain) and reuses the real providers to fetch/verify.

Usage::

    .venv/bin/python scripts/capture_ats_host.py
    .venv/bin/python scripts/build_registry.py scripts/candidates_atshost.json
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

from harvest_tokens import name_match  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
OUT = ROOT / "scripts" / "candidates_atshost.json"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# (giant company key, brand for adjudication, careers URLs to load)
CORPS = [
    (
        "twosigmainvestments",
        "two sigma",
        ["https://careers.twosigma.com/", "https://careers.twosigma.com/jobs"],
    ),
    (
        "depositorytrustandclearing",
        "dtcc",
        ["https://careers.dtcc.com/", "https://www.dtcc.com/careers"],
    ),
    (
        "fujitsunorthamerica",
        "fujitsu",
        ["https://careers.fujitsu.com/en", "https://www.fujitsu.com/global/about/careers/"],
    ),
    (
        "schlumbergertechnology",
        "schlumberger slb",
        ["https://careers.slb.com/", "https://careers.slb.com/search-jobs"],
    ),
    ("creditkarma", "credit karma", ["https://www.creditkarma.com/careers/openings"]),
    ("slalom", "slalom", ["https://www.slalom.com/us/en/careers/find-a-job"]),
    (
        "tiktokusdatasecurity",
        "tiktok usds",
        ["https://careers.usds.tiktok.com/", "https://careers.usds.tiktok.com/search"],
    ),
    (
        "cgitechnologiesandsolutions",
        "cgi",
        ["https://www.cgi.com/en/careers/search-careers", "https://cgi.njoyn.com"],
    ),
    (
        "skyworkssolutions",
        "skyworks",
        ["https://www.skyworksinc.com/en/Careers", "https://careers.skyworksinc.com/"],
    ),
    ("kpittechnologies", "kpit", ["https://www.kpit.com/careers/job-openings/"]),
    (
        "landttechnologyservices",
        "l&t technology services ltts",
        ["https://www.ltts.com/careers", "https://careers.ltts.com"],
    ),
    ("groupmworldwide", "groupm wpp", ["https://www.groupm.com/careers/"]),
]

_WD = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/([^/]+)/|(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+))",
    re.I,
)
_OTHER = {
    "avature": re.compile(r"https?://([a-z0-9-]+\.avature\.net)", re.I),
    "icims": re.compile(r"https?://([a-z0-9-]+\.icims\.com)", re.I),
    "phenom": re.compile(r"https?://([a-z0-9-]+\.phenompeople\.com)", re.I),
    "eightfold": re.compile(r"https?://([a-z0-9-]+)\.eightfold\.ai", re.I),
}


def _extract(url: str) -> tuple[str, str] | None:
    """Return (ats, token) if a request URL is a recognizable ATS host call."""
    m = _WD.search(url)
    if m:
        tenant, wd = m.group(1).lower(), m.group(2).lower()
        site = m.group(3) or m.group(4)
        if site and site.lower() not in ("en-us", "wday"):
            return "workday", f"{tenant}|{wd}|{site}"
    for ats, pat in _OTHER.items():
        mm = pat.search(url)
        if mm:
            host = mm.group(1).lower()
            return ats, (host if ats != "eightfold" else mm.group(1).lower())
    return None


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return

    hits: dict[str, list[tuple[str, str]]] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(4)

        async def grab(ck: str, brand: str, urls: list[str]) -> None:
            found: list[tuple[str, str]] = []
            async with limiter:
                try:
                    ctx = await browser.new_context(user_agent=_UA)
                except Exception:  # noqa: BLE001
                    return
                try:
                    for u in urls:
                        page = await ctx.new_page()

                        def on_req(r: object) -> None:
                            hit = _extract(getattr(r, "url", ""))
                            if hit and hit not in found:
                                found.append(hit)

                        page.on("request", on_req)
                        try:
                            await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                            await page.wait_for_timeout(7000)
                        except Exception:  # noqa: BLE001
                            pass
                        await page.close()
                        if found:
                            break
                finally:
                    await ctx.close()
            if found:
                hits[ck] = found
                print(f"  {ck:28} -> {found}", flush=True)
            else:
                print(f"  {ck:28} -- no ATS host request seen", flush=True)

        async with anyio.create_task_group() as tg:
            for ck, brand, urls in CORPS:
                tg.start_soon(grab, ck, brand, urls)
        await browser.close()

    # Verify each captured (ats, token) through the provider; adjudicate shared-host by name.
    brand_by_ck = {ck: b for ck, b, _ in CORPS}
    candidates: list[dict] = []
    async with AsyncFetcher(concurrency=6, per_host_rate=4, timeout=25.0, retries=1) as f:
        for ck, found in hits.items():
            for ats, token in found:
                try:
                    raws = await get_provider(ats).fetch(token, SearchQuery(limit=5), f)
                except Exception:  # noqa: BLE001
                    raws = []
                if not raws:
                    continue
                # workday/oracle expose no display name -> trust (host came from the company's OWN
                # page). avature/icims/phenom/eightfold: require a name match on the board company.
                board_co = raws[0].company or ""
                ok = ats == "workday" or name_match(brand_by_ck[ck], board_co)
                print(
                    f"  verify {ck:26} {ats:9} {token[:34]:34} {len(raws)}j co={board_co[:18]} ok={ok}"
                )
                if ok:
                    c = {"company": ck, "ats": ats, "domain": None}
                    if ats == "workday":
                        t, w, s = token.split("|", 2)
                        c.update({"tenant": t, "wd": w, "site": s})
                    else:
                        c["token"] = token
                    candidates.append(c)
                    break

    OUT.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(candidates)} ATS-host candidates -> {OUT.name}")


if __name__ == "__main__":
    anyio.run(main)
