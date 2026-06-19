"""Capture the EXACT job-records request a JS careers page makes (method, url, headers, body,
records-path, sample fields) — everything needed to hand-build an apicapture/coveo-direct spec.

The proven recipe (UST, TikTok): find the real API host, capture the browser's exact request,
replicate statically with HTTP/1.1 + browser-UA. This tool does the capture step for a batch of
giants, dumping a ready-to-configure summary.

Usage::

    .venv/bin/python scripts/capture_request.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from capture_api import _find_records  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

from census_successfactors import tavily  # noqa: E402  # isort: skip

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# label -> (Tavily query, fallback URLs)
TARGETS = {
    "dtcc": ("DTCC careers search jobs openings", ["https://careers.dtcc.com/search-jobs"]),
    "fujitsu": (
        "Fujitsu careers job search apply",
        ["https://www.fujitsu.com/global/about/careers/"],
    ),
    "cgi": ("CGI careers search jobs", ["https://www.cgi.com/en/careers/search-careers"]),
    "kpit": ("KPIT careers job openings search", ["https://www.kpit.com/careers/job-openings/"]),
    "creditkarma": (
        "Credit Karma careers openings job search",
        ["https://www.creditkarma.com/careers/openings"],
    ),
    "fiserv": ("Fiserv careers job search", ["https://careers.fiserv.com/"]),
}
_DENY = (
    "paradox",
    "bebee",
    "jobdiva",
    "chronicle",
    "consentmo",
    "indeed",
    "linkedin",
    "securiti",
    "cookie",
)


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return
    from ergon_tracker.http import AsyncFetcher

    key = load_key()
    urls_by: dict[str, list[str]] = {}
    async with (
        AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=2) as tav,
        anyio.create_task_group() as tg,
    ):

        async def resolve(label: str, q: str, fb: list[str]) -> None:
            try:
                found = await tavily(q, key, tav) if key else []
            except Exception:  # noqa: BLE001
                found = []
            urls_by[label] = [u for u in found if u.startswith("http")][:3] + fb

        for label, (q, fb) in TARGETS.items():
            tg.start_soon(resolve, label, q, fb)

    findings: dict[str, dict] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(3)

        async def grab(label: str, urls: list[str]) -> None:
            async with limiter:
                ctx = await browser.new_context(user_agent=_UA)
                hit: dict = {}
                try:
                    for u in urls:
                        page = await ctx.new_page()

                        async def on_resp(r: object) -> None:
                            url = getattr(r, "url", "")
                            if any(d in url.lower() for d in _DENY):
                                return
                            try:
                                ct = (await r.header_value("content-type")) or ""  # type: ignore[attr-defined]
                                if "json" not in ct.lower():
                                    return
                                data = await r.json()  # type: ignore[attr-defined]
                            except Exception:  # noqa: BLE001
                                return
                            rec = _find_records(data)
                            if rec and len(rec[1]) >= 3 and not hit:
                                req = r.request  # type: ignore[attr-defined]
                                sample = rec[1][0]
                                hit.update(
                                    {
                                        "method": req.method,
                                        "url": url,
                                        "headers": {
                                            k: v
                                            for k, v in req.headers.items()
                                            if k.lower()
                                            in (
                                                "content-type",
                                                "website-path",
                                                "portal-channel",
                                                "x-api-key",
                                                "authorization",
                                                "referer",
                                                "x-csrf-token",
                                            )
                                        },
                                        "body": req.post_data,
                                        "records_path": rec[0],
                                        "count": len(rec[1]),
                                        "sample_keys": list(sample.keys())[:14]
                                        if isinstance(sample, dict)
                                        else str(type(sample)),
                                    }
                                )

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
                        if hit:
                            hit["page"] = u
                            break
                finally:
                    await ctx.close()
            findings[label] = hit
            print(f"  {label:14} -> {hit.get('url', 'NO job-records request')[:70]}", flush=True)

        async with anyio.create_task_group() as tg:
            for label, urls in urls_by.items():
                tg.start_soon(grab, label, urls)
        await browser.close()

    print("\n=== captured requests (ready for spec-building) ===")
    print(json.dumps(findings, indent=2)[:4000])


if __name__ == "__main__":
    anyio.run(main)
