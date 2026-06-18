"""Recover "proxied/opaque" H-1B giants by EXTRACTING the ATS host from their careers page.

The census labelled ~364 mega-sponsors (100k filings) "proxied/opaque" because matching their
Tavily *result URLs* found no ATS — their careers site is on their own domain (jobs.citi.com,
careers.jpmorgan.com). But the real ATS host is often embedded in that page's HTML/redirects
(citi -> citi.eightfold.ai, jpmorgan -> an oraclecloud host). So per giant:

  1. Tavily-find the careers URL.
  2. FETCH it and regex out any supported-ATS host (eightfold/workday/oracle/icims/taleo/
     successfactors/phenom/brassring/avature). For Workday, follow the host to its default
     career-site so we get the {tenant}|{wd}|{site} token.
  3. Verify live through the provider and adjudicate the board's company name against the
     sponsor (guards a careers page that merely *links* to an unrelated board). Emit candidates.

What this can't reach (JS-injected hosts that never appear in static HTML) is left for a
Playwright capture pass. Heavy concurrency + fail-fast fetcher (most probes 404/NXDOMAIN).

Usage::

    .venv/bin/python scripts/discover_giants.py [--cap N] [--out scripts/candidates_giants.json]
    .venv/bin/python scripts/build_registry.py scripts/candidates_giants.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from census_successfactors import tavily  # noqa: E402
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core, name_match  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
GIANTS = ROOT / "runs" / "giants.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_giants.json"
EXCLUDE = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "wikipedia.org",
    "myvisajobs.com",
    "h1bdata.info",
    "google.com",
    "facebook.com",
    "youtube.com",
)

# Full-host extractors per ATS. Order = preference. Each yields a careers URL/host that the
# provider's matches() turns into a token.
_EIGHTFOLD = re.compile(r"([a-z0-9][a-z0-9-]*\.eightfold\.ai)", re.I)
# Capture the FULL Workday URL incl. the /{site} or /en-US/{site} path (the site is what
# matches() needs; the bare host alone can't be resolved to a token).
_WORKDAY_FULL = re.compile(
    r"([a-z0-9][a-z0-9-]*\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_]+)", re.I
)
_WORKDAY = re.compile(r"([a-z0-9][a-z0-9-]*\.wd\d+\.myworkdayjobs\.com)", re.I)
_ORACLE = re.compile(r"([a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com)", re.I)
_ICIMS = re.compile(r"(careers-[a-z0-9-]+\.icims\.com|[a-z0-9-]+\.icims\.com)", re.I)
_TALEO = re.compile(r"([a-z0-9-]+\.taleo\.net)", re.I)
_PHENOM = re.compile(r"([a-z0-9-]+\.phenompeople\.com)", re.I)
_AVATURE = re.compile(r"([a-z0-9-]+\.avature\.net)", re.I)


def _careers_urls(urls: list[str]) -> list[str]:
    return [u for u in urls if u and not any(x in u.lower() for x in EXCLUDE)][:3]


async def _workday_token(host: str, fetcher: AsyncFetcher) -> str | None:
    """A Workday host needs {tenant}|{wd}|{site}; follow the host to its default site URL."""
    wd = get_provider("workday")
    for path in ("/", "/en-US/"):
        try:
            resp = await fetcher.request("GET", f"https://{host}{path}", timeout=8.0)
        except Exception:  # noqa: BLE001
            continue
        tok = wd.matches(str(resp.url))
        if tok:
            return tok
    return None


async def detect(sponsor: str, html: str, fetcher: AsyncFetcher) -> tuple[str, str] | None:
    """Extract the first supported-ATS token from a careers page's HTML.

    Workday is checked BEFORE Eightfold: on giants that run both (e.g. Citi), Workday is the
    canonical full board while Eightfold is usually a "Match Me" overlay returning a partial set.
    """
    m = _WORKDAY_FULL.search(html)
    if m:
        tok = get_provider("workday").matches("https://" + m.group(1))
        if tok:
            return "workday", tok
    m = _WORKDAY.search(html)  # host only -> resolve the site by following the host
    if m:
        tok = await _workday_token(m.group(1).lower(), fetcher)
        if tok:
            return "workday", tok
    m = _EIGHTFOLD.search(html)
    if m:
        tok = get_provider("eightfold").matches(m.group(1))
        if tok:
            return "eightfold", tok
    for ats, pat in (
        ("oracle", _ORACLE),
        ("icims", _ICIMS),
        ("taleo", _TALEO),
        ("phenom", _PHENOM),
        ("avature", _AVATURE),
    ):
        m = pat.search(html)
        if m:
            tok = get_provider(ats).matches("https://" + m.group(1))
            if tok:
                return ats, tok
    return None


async def discover(sponsor: str, key: str, fetcher: AsyncFetcher) -> tuple[str, str] | None:
    urls = await tavily(sponsor, key, fetcher)
    for u in _careers_urls(urls):
        try:
            resp = await fetcher.request("GET", u, timeout=12.0)
        except Exception:  # noqa: BLE001
            continue
        hit = await detect(sponsor, resp.text or "", fetcher)
        if hit:
            return hit
    return None


def _company_key(sponsor: str, ats: str, token: str) -> str:
    host = token.split("|", 1)[0]
    if ats in ("eightfold", "jazzhr", "jobvite"):
        return token.split("|", 1)[0]
    if ats in ("taleo", "avature", "phenom"):
        return host.split(".")[0]
    if ats == "successfactors":
        return token.split("|", 1)[1]
    return _core(sponsor) or host.split(".")[0]  # workday/oracle/icims -> sponsor core


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
    giants = json.loads(GIANTS.read_text())["uncovered_top"][:cap]
    seed_keys = load_seed_keys()
    print(f"discovering ATS host for {len(giants)} proxied giants ...", flush=True)

    hits: dict[int, tuple[str, str]] = {}
    done = [0]

    async def find(idx: int, g: dict, tav: AsyncFetcher, probe: AsyncFetcher) -> None:
        urls = await tavily(g["name"], key, tav)
        for u in _careers_urls(urls):
            try:
                resp = await probe.request("GET", u, timeout=12.0)
            except Exception:  # noqa: BLE001
                continue
            hit = await detect(g["name"], resp.text or "", probe)
            if hit:
                hits[idx] = hit
                break
        done[0] += 1
        if done[0] % 50 == 0:
            print(f"  scanned {done[0]}/{len(giants)} (hits: {len(hits)})", flush=True)

    async with (
        AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=3) as tav,
        AsyncFetcher(concurrency=24, per_host_rate=10, timeout=12.0, retries=1) as probe,
        anyio.create_task_group() as tg,
    ):
        for idx, g in enumerate(giants):
            tg.start_soon(find, idx, g, tav, probe)

    print(
        f"detected {len(hits)} {dict(Counter(a for a, _ in hits.values()))}; verifying ...",
        flush=True,
    )

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
        # Adjudicate the board company against the sponsor (workday/eightfold expose no display
        # name -> trust the careers-page provenance; others require a name match).
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
            if ats == "workday":  # build_registry wants split tenant|wd|site, not a composite token
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
        print(
            f"  + {c['_sponsor'][:28]:28} ({c.get('_filings') or 0:>5}) {c['ats']:12} {c['token']}"
        )
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(
        f"\nwrote {len(out)} candidates {dict(Counter(c['ats'] for c in candidates))} -> {out_path.name}"
    )


if __name__ == "__main__":
    anyio.run(main)
