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

from census_successfactors import (  # noqa: E402
    _domain_label,
    _is_sf,
    tavily,  # noqa: E402
)
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core, name_match  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, iter_providers, load_builtins  # noqa: E402

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
# Every absolute URL in the page — we run each provider's matches() over all of them so EVERY
# supported ATS is covered (SF/brassring/jobvite/jazzhr/greenhouse/lever/... not just the 7 hosts).
_ALL_URLS = re.compile(r"https?://[^\s\"'<>)]+", re.I)
# Provider check order: canonical full boards first, path-based last. Aggregators excluded.
_ORDER = (
    "workday",
    "oracle",
    "icims",
    "taleo",
    "successfactors",
    "brassring",
    "phenom",
    "avature",
    "jobvite",
    "jazzhr",
    "eightfold",
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "recruitee",
    "personio",
    "bamboohr",
    "rippling",
    "pinpoint",
    "breezy",
    "teamtailor",
)
_SF_SIG = ("jobs2web", "successfactors", "/services/rss/job", "rmkcdn", "sapsf")


_STOP = (
    "the",
    "inc",
    "llc",
    "corp",
    "co",
    "and",
    "services",
    "service",
    "group",
    "company",
    "technologies",
    "technology",
    "llp",
    "ltd",
    "lp",
    "usa",
    "us",
    "global",
    "international",
)
# A job-search sublink to follow when the landing page has no ATS host.
_SUBLINK = re.compile(r'href="(https?://[^"]*(?:search|/jobs?|find-?jobs|careers?)[^"]*)"', re.I)


def _slug(name: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split() if w not in _STOP]
    return words[0] if words else ""


# ATSes whose tenant/site identifier SHOULD relate to the sponsor (citi.eightfold.ai,
# accenture|wd103, careers.wipro.com|wipro). This rejects a careers page that embeds a THIRD-
# PARTY ATS host (a partner board, or a Coveo `/rest/search` widget that SF.matches false-hits).
# Shared-host ATSes (greenhouse/lever/...) and opaque-host Oracle are verified by name-match /
# careers-page provenance instead, so they're exempt.
_RELATE_ATS = {"workday", "eightfold", "icims", "taleo", "avature", "phenom", "successfactors"}


def _identifier(ats: str, token: str) -> str:
    parts = token.split("|")
    if ats == "successfactors" and len(parts) > 1:
        return parts[1].lower()  # siteid
    if ats == "workday":
        return parts[0].lower()  # tenant
    return parts[0].split(".")[0].lower()  # eightfold token / host label


def _relates(sponsor: str, ats: str, token: str) -> bool:
    if ats not in _RELATE_ATS:
        return True
    ident = _identifier(ats, token)
    core = _core(sponsor)
    if not ident or not core:
        return True
    return ident in core or core in ident or core[:5] == ident[:5] or name_match(sponsor, ident)


def _candidate_urls(sponsor: str, tavily_urls: list[str]) -> list[str]:
    """Tavily careers results PLUS guessed hosts (careers./jobs./{domain}) — many giants'
    real ATS host only appears on the careers subdomain, not whatever Tavily returned first."""
    out = [u for u in tavily_urls if u and not any(x in u.lower() for x in EXCLUDE)][:3]
    s = _slug(sponsor)
    if len(s) >= 3:
        for guess in (
            f"careers.{s}.com",
            f"jobs.{s}.com",
            f"www.{s}.com/careers",
            f"{s}.com/careers",
            f"careers.{s}.com/jobs",
        ):
            u = "https://" + guess
            if u not in out:
                out.append(u)
    return out


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
    """Extract a supported-ATS token from a careers page's HTML — comprehensively.

    Runs EVERY provider's ``matches()`` over EVERY URL in the page (priority order: canonical
    full boards first; Workday/Oracle before Eightfold, which on dual-stack giants is just a
    partial "Match Me" overlay). Then host-signature fallbacks for ATSes whose token needs more
    than a URL: a Workday host with no full URL (resolve the site), and a SuccessFactors host
    (RSS-confirm + domain-label siteid).
    """
    urls = _ALL_URLS.findall(html)
    provs = {p.name: p for p in iter_providers()}
    for name in _ORDER:
        prov = provs.get(name)
        if prov is None:
            continue
        for u in urls:
            try:
                tok = prov.matches(u)
            except Exception:  # noqa: BLE001
                tok = None
            if tok and _relates(sponsor, name, tok):
                return name, tok

    # Fallback A: a bare Workday host (no /{site} URL) -> follow it to the default site.
    m = _WORKDAY.search(html)
    if m:
        tok = await _workday_token(m.group(1).lower(), fetcher)
        if tok:
            return "workday", tok
    # Fallback B: a SuccessFactors careers host (signature present but no /{siteid}/ URL) ->
    # RSS-confirm the careers host itself and use its domain label as the siteid.
    low = html.lower()
    if any(s in low for s in _SF_SIG):
        core = _core(sponsor)
        for u in urls[:30]:
            host = re.sub(r"^https?://", "", u).split("/")[0].split(":")[0].lower()
            # only probe a host that belongs to the sponsor (its slug is in the host) — skip
            # third-party search/CDN hosts (coveo, cloudfront, ...) that also answer the RSS path.
            label = host.split(".")[0]
            if not host or not (core and (core in host or label in core)):
                continue
            if await _is_sf(host, fetcher):
                return "successfactors", f"{host}|{_domain_label(host)}"
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
        for u in _candidate_urls(g["name"], urls):
            try:
                resp = await probe.request("GET", u, timeout=12.0)
            except Exception:  # noqa: BLE001
                continue
            html = resp.text or ""
            hit = await detect(g["name"], html, probe)
            if hit is None:  # landing has no ATS host -> follow one job-search subpage
                sm = _SUBLINK.search(html)
                if sm and sm.group(1) != u:
                    try:
                        sub = await probe.request("GET", sm.group(1), timeout=12.0)
                        hit = await detect(g["name"], sub.text or "", probe)
                    except Exception:  # noqa: BLE001
                        pass
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
