"""Broad residual sweep: for every residual giant, find its careers page and detect ANY reachable
board (all ATS hosts + own-domain /api/jobs + coveo + phenom /widgets), using a browser UA +
HTTP/1.1 to defeat the anti-bot 403s that blocked bot-UA passes. Reports capturable hits."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from census_successfactors import tavily
from harvest_commoncrawl import load_seed_keys
from harvest_tavily import load_key
from harvest_tokens import _core

from ergon_tracker.http import AsyncFetcher

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ATS = re.compile(
    r'([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)|([a-z0-9-]+\.icims\.com)|([a-z0-9-]+\.taleo\.net)|([a-z0-9-]+\.successfactors\.(?:com|eu))|([a-z0-9-]+\.avature\.net)|([a-z0-9-]+\.phenompeople\.com)|([a-z0-9-]+\.oraclecloud\.com)|([a-z0-9-]+\.peopleadmin\.com)|(boards\.greenhouse\.io/[a-z0-9]+)|(jobs\.lever\.co/[a-z0-9-]+)|(jobs\.ashbyhq\.com/[a-z0-9-]+)|([a-z0-9-]+\.smartrecruiters\.com)|(/coveo/rest/search)|(/api/v\d+/search/job)|(jibeApiDomain|/api/jobs)|([a-z0-9-]+\.njoyn\.com)|(/widgets["\'][^>]*refineSearch)|([a-z0-9-]+\.csod\.com)|(workforcenow\.adp\.com)|(\.jobvite\.com)|(sjobs\.brassring\.com)',
    re.I,
)


async def main():
    key = load_key()
    giants = json.loads((ROOT / "runs/giants.json").read_text())["uncovered_top"]
    sk = load_seed_keys()
    resid = [g for g in giants if _core(g["name"]) not in sk]
    resid.sort(key=lambda g: -(g.get("filings") or 0))
    print(f"sweeping {len(resid)} residual giants (browser UA + http1)...", flush=True)
    urls_by = {}
    async with AsyncFetcher(concurrency=8, per_host_rate=4, timeout=14.0, retries=2) as tav:
        async with anyio.create_task_group() as tg:

            async def res(g):
                try:
                    urls_by[g["name"]] = await tavily(f"{g['name']} careers jobs", key, tav)
                except Exception:
                    urls_by[g["name"]] = []

            for g in resid:
                tg.start_soon(res, g)
    hits = []
    done = [0]
    limiter = anyio.CapacityLimiter(10)

    async def probe(g):
        async with limiter:
            name = g["name"]
            urls = [
                u
                for u in urls_by.get(name, [])
                if u.startswith("http")
                and not any(
                    x in u
                    for x in (
                        "linkedin",
                        "indeed",
                        "glassdoor",
                        "zoominfo",
                        "naukri",
                        "ziprecruiter",
                        "wikipedia",
                    )
                )
            ][:3]
            found = set()
            sublink = re.compile(
                r'href="(https?://[^"]*(?:career|job|search|opening|position|vacanc|employ)[^"]*)"',
                re.I,
            )
            async with httpx.AsyncClient(
                timeout=12.0, follow_redirects=True, http2=False, headers={"User-Agent": UA}
            ) as c:
                for u in urls:
                    try:
                        r = await c.get(u)
                    except Exception:
                        continue
                    blob = (str(r.url) + " " + (r.text or ""))[:400000]
                    for m in ATS.findall(blob):
                        v = next((x for x in m if x), "")
                        if v:
                            found.add(v.lower())
                    # one level deeper: follow a careers/jobs sublink on the SAME host (the board
                    # host is often on a "search jobs" subpage, not the careers landing).
                    if not found:
                        base = str(r.url).split("/")[2] if "//" in str(r.url) else ""
                        subs = [s for s in sublink.findall(r.text or "") if base in s and s != u][
                            :2
                        ]
                        for s in subs:
                            try:
                                rs = await c.get(s)
                            except Exception:
                                continue
                            for m in ATS.findall((str(rs.url) + " " + (rs.text or ""))[:300000]):
                                v = next((x for x in m if x), "")
                                if v:
                                    found.add(v.lower())
                            if found:
                                break
                    if found:
                        break
            done[0] += 1
            if done[0] % 30 == 0:
                print(f"  {done[0]}/{len(resid)} (hits {len(hits)})", flush=True)
            if found:
                hits.append((g.get("filings", 0), name, sorted(found)[:3]))

    async with anyio.create_task_group() as tg:
        for g in resid:
            tg.start_soon(probe, g)
    hits.sort(key=lambda x: -x[0])
    print(f"\n=== {len(hits)} residual giants with a detected board ===")
    for fil, name, f in hits:
        print(f"  {fil:>5} {name[:36]:36} {f}")


anyio.run(main)
