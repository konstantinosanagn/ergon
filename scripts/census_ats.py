"""Unified ATS census over the H-1B gap: ONE Tavily pass per sponsor, detect every
direct-host provider we support (Taleo, Oracle Recruiting Cloud, iCIMS, SuccessFactors).

The Eightfold/SF censuses each did their own Tavily sweep; doing one pass that detects all
four ATSes at once is far more Tavily-quota-efficient and finds boards across providers in a
single run. Per sponsor:

  1. Tavily-search ``{sponsor} careers`` once.
  2. Detect, cheapest-first:
     (a) ``provider.matches()`` on each result URL — catches the host-based ATSes whose
         careers URL Tavily returns directly (``*.taleo.net``, ``*.fa.*.oraclecloud.com``,
         ``*.icims.com``, vanity iCIMS URLs with an iCIMS path).
     (b) Otherwise probe the careers hosts: iCIMS vanity domains answer ``/api/jobs`` with
         JSON; SuccessFactors hosts answer ``/services/rss/job/`` with RSS XML.
  3. Verify live (limit=1) through the matched provider; only boards returning >=1 job and
     not already seeded are emitted as build_registry candidates.

Usage::

    .venv/bin/python scripts/census_ats.py [--out scripts/candidates_ats.json] [--cap N]
    .venv/bin/python scripts/build_registry.py scripts/candidates_ats.json
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
    GAP,
    MIDTIER,
    _domain_label,
    _is_sf,
    _sf_candidate_hosts,
    tavily,
)
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from ergon_tracker.providers.icims import ICIMSProvider  # noqa: E402
from ergon_tracker.providers.phenom import PhenomProvider  # noqa: E402

load_builtins()
DEFAULT_OUT = ROOT / "scripts" / "candidates_ats.json"
_ICIMS_API = "https://{host}/api/jobs?page=1&limit=1"
# Host-based providers whose matches() recognises a careers URL directly (order = preference).
# Each is host-specific (*.taleo.net, *.oraclecloud.com, *.icims.com, *.avature.net,
# *.applytojob.com, jobs.jobvite.com, *.phenompeople.com) so order never causes mis-matches.
_URL_ATSES = ("taleo", "oracle", "icims", "avature", "jazzhr", "jobvite", "phenom")


def _slug(name: str) -> str:
    """A clean registry key from a sponsor name (drops legal-form noise)."""
    words = [
        w
        for w in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
        if w not in ("the", "inc", "llc", "corp", "co", "ltd", "lp", "llp", "usa", "us", "and")
    ]
    return "".join(words[:3]) or re.sub(r"[^a-z0-9]", "", name.lower())[:20]


def _company_key(sponsor: str, ats: str, token: str) -> str:
    """The registry key for a board — natural per-ATS, so re-runs dedup against the seed."""
    host = token.split("|", 1)[0]
    if ats == "successfactors":
        return token.split("|", 1)[1]  # siteid (careers.ey.com|ey -> ey)
    if ats == "taleo":
        return host.split(".")[0]  # tenant label (drhorton.taleo.net -> drhorton)
    if ats == "icims":
        return ICIMSProvider._host_company(host)  # careers-winco.icims.com -> winco
    if ats == "phenom":
        return PhenomProvider._host_company(host)  # careers.activisionblizzard.com -> activision...
    if ats == "avature":
        return host.split(".")[0]  # bloomberg.avature.net -> bloomberg
    if ats in ("jazzhr", "jobvite"):
        return token  # token IS the slug/subdomain (firstadvantage, buckman)
    return _slug(sponsor)  # oracle hosts are opaque codes -> use the sponsor slug


async def detect(urls: list[str], fetcher: AsyncFetcher) -> tuple[str, str] | None:
    """Return ``(ats, token)`` for the first provider that recognises the sponsor, else None."""
    # (a) direct matches() on result URLs for the host-based ATSes
    for u in urls:
        for ats in _URL_ATSES:
            tok = get_provider(ats).matches(u)
            if tok:
                return ats, tok
    # (b) probe careers hosts for the vanity-domain providers via cheap GETs: iCIMS (/api/jobs
    # JSON) then SuccessFactors (RSS). NOTE: Phenom is intentionally NOT probed here — a blind
    # POST /widgets to arbitrary careers hosts hangs many of them to the timeout (~20x slowdown
    # for 0.2% of the gap), so Phenom is caught only via matches() on deep Tavily links above.
    for host in _sf_candidate_hosts(urls)[:5]:
        try:
            resp = await fetcher.request("GET", _ICIMS_API.format(host=host), timeout=8.0)
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", "").lower():
                body = resp.json()
                if isinstance(body, dict) and ("jobs" in body or "totalCount" in body):
                    return "icims", host
        except Exception:  # noqa: BLE001
            pass
        if await _is_sf(host, fetcher):
            return "successfactors", f"{host}|{_domain_label(host)}"
    return None


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    cap = 2500
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

    sponsors: dict[str, dict] = {}
    for f in (GAP, MIDTIER):
        if f.exists():
            for s in json.loads(f.read_text()).get("uncovered_top", []):
                cur = sponsors.get(s["name"])
                if cur is None or s.get("filings", 0) > cur.get("filings", 0):
                    sponsors[s["name"]] = s
    todo = sorted(sponsors.values(), key=lambda s: -s.get("filings", 0))[:cap]
    seed_keys = load_seed_keys()
    print(f"unified ATS census over {len(todo)} uncovered sponsors ...", flush=True)

    # Phase 1: Tavily + detect (concurrent).
    hits: dict[int, tuple[str, str]] = {}
    done = [0]

    # Two fetchers: Tavily needs retries to survive 429s, but the careers-host PROBES must NOT
    # retry — most guessed hosts (careers.{dom}/jobs.{dom}) don't resolve, and retrying a
    # TransportError 3x with backoff was the bottleneck (~15s wasted per dead guess). retries=1
    # makes dead hosts fail instantly; a short timeout caps slow-but-alive ones.
    async def find(idx: int, sponsor: dict, tav: AsyncFetcher, probe: AsyncFetcher) -> None:
        urls = await tavily(sponsor["name"], key, tav)
        res = await detect(urls, probe)
        if res:
            hits[idx] = res
        done[0] += 1
        if done[0] % 200 == 0:
            print(f"  scanned {done[0]}/{len(todo)} (hits: {len(hits)})", flush=True)

    async with (
        AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=3) as tav,
        AsyncFetcher(concurrency=16, per_host_rate=8, timeout=6.0, retries=1) as probe,
        anyio.create_task_group() as tg,
    ):
        for idx, s in enumerate(todo):
            tg.start_soon(find, idx, s, tav, probe)

    by_ats = Counter(ats for ats, _ in hits.values())
    print(f"detected {len(hits)} candidates {dict(by_ats)}; verifying live ...", flush=True)

    # Phase 2: verify each live (limit=1) through its provider; keep live + unseeded.
    candidates: list[dict] = []
    taken: set[str] = set()

    async def verify(idx: int, ats: str, token: str, fetcher: AsyncFetcher) -> None:
        sponsor = todo[idx]["name"]
        ck = _company_key(sponsor, ats, token).lower()
        if not ck or ck in seed_keys or ck in taken:
            return
        try:
            raws = await get_provider(ats).fetch(token, SearchQuery(limit=1), fetcher)
        except Exception:  # noqa: BLE001
            raws = []
        if raws:
            taken.add(ck)
            candidates.append(
                {
                    "company": ck,
                    "ats": ats,
                    "token": token,
                    "domain": None,
                    "_sponsor": sponsor,
                    "_filings": todo[idx].get("filings"),
                }
            )

    async with (
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=25.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, (ats, token) in hits.items():
            tg.start_soon(verify, idx, ats, token, fetcher)

    candidates.sort(key=lambda c: -(c.get("_filings") or 0))
    kept = Counter(c["ats"] for c in candidates)
    for c in candidates:
        print(
            f"  + {c['_sponsor'][:30]:30} ({c.get('_filings') or 0:>5}) {c['ats']:14} {c['token']}"
        )
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"\nwrote {len(out)} candidates {dict(kept)} -> {shown}")
    print(f"next: .venv/bin/python scripts/build_registry.py {shown}")


if __name__ == "__main__":
    anyio.run(main)
