"""Census the H-1B gap for SuccessFactors career sites -> verified candidates.json.

The SuccessFactors provider exists, so any uncovered H-1B sponsor on SuccessFactors is
recoverable now with zero new code — we just need to find each sponsor's career host +
siteid. Unlike Eightfold, SF sites are server-rendered HTML on the company's OWN domain
(careers.{co}.com/{siteid}/...), so detection is browser-free:

  1. Tavily-search ``{sponsor} careers`` (aggregators excluded) for the company's pages.
  2. Detect SF two ways: (a) a result URL is already an SF job/search URL
     (SuccessFactorsProvider.matches gives the exact siteid), or (b) RSS-confirm a candidate
     SF host — the ``careers.``/``jobs.`` subdomain of a result domain answers
     ``/services/rss/job/`` with RSS XML (404 otherwise: fast + definitive) — and use the
     domain label as the siteid (careers.ey.com -> ey).
  3. Verify gate: fetch live (limit=1) through the provider. Only boards returning >=1 job
     and not already seeded are emitted, so a wrong siteid guess is dropped, not merged.

Usage::

    .venv/bin/python scripts/census_successfactors.py [--out scripts/candidates_sf.json] [--cap N]
    .venv/bin/python scripts/build_registry.py scripts/candidates_sf.json
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

from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

from ergon.http import AsyncFetcher  # noqa: E402
from ergon.models import SearchQuery  # noqa: E402
from ergon.providers.successfactors import SuccessFactorsProvider  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_sf.json"
GAP = ROOT / "runs" / "h1b_coverage_gap.json"
MIDTIER = ROOT / "runs" / "h1b_gap_midtier.json"
_API = "https://api.tavily.com/search"
EXCLUDE = [
    "linkedin.com",
    "indeed.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "wikipedia.org",
    "levels.fyi",
    "builtin.com",
    "simplyhired.com",
    "monster.com",
    "dice.com",
    "myvisajobs.com",
    "h1bdata.info",
    "trackitt.com",
    "salary.com",
    "google.com",
]
_RSS = "https://{host}/services/rss/job/"
_SF = SuccessFactorsProvider()


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _domain_label(host: str) -> str:
    """Registrable-domain main label = the usual SF siteid (careers.ey.com->ey, jobs.sap.com->sap)."""
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def _sf_candidate_hosts(urls: list[str]) -> list[str]:
    """SF host candidates from the sponsor's result URLs: the careers/jobs subdomains of each
    result domain (the SF site usually lives there, e.g. www.ey.com -> careers.ey.com), then the
    raw result hosts. Aggregators excluded; ``careers.``/``jobs.`` tried first."""
    hosts: list[str] = []
    raw: list[str] = []
    for u in urls:
        h = _host_of(u)
        if not h or any(x in h for x in EXCLUDE):
            continue
        dom = ".".join(h.split(".")[-2:])
        for guess in (f"careers.{dom}", f"jobs.{dom}"):
            if guess not in hosts:
                hosts.append(guess)
        if h not in raw:
            raw.append(h)
    return hosts + [h for h in raw if h not in hosts]


async def tavily(sponsor: str, key: str, fetcher: AsyncFetcher) -> list[str]:
    body = {"query": f"{sponsor} careers jobs", "exclude_domains": EXCLUDE, "max_results": 5}
    try:
        data = await fetcher.post_json(_API, json=body, headers={"Authorization": f"Bearer {key}"})
    except Exception:  # noqa: BLE001
        return []
    return [r.get("url", "") for r in (data.get("results", []) if isinstance(data, dict) else [])]


async def _is_sf(host: str, fetcher: AsyncFetcher) -> bool:
    """A SuccessFactors host answers ``/services/rss/job/`` with RSS XML (404 otherwise).

    This is fast, small, and definitive — far more reliable than scraping a slow homepage.
    """
    try:
        resp = await fetcher.request(
            "GET", _RSS.format(host=host), params={"keywords": "()"}, timeout=8.0
        )
    except Exception:  # noqa: BLE001
        return False
    return resp.status_code == 200 and "xml" in resp.headers.get("content-type", "").lower()


async def detect(sponsor: str, urls: list[str], fetcher: AsyncFetcher) -> str | None:
    """Return an SF ``"{host}|{siteid}"`` token for the sponsor, else None.

    (a) An exact SF job/search URL in the results gives the precise siteid. (b) Otherwise
    RSS-confirm a candidate SF host (careers./jobs. subdomain of a result domain) and use the
    domain label as the siteid. The verify gate (provider.fetch) validates the siteid, so a
    wrong guess is dropped rather than merged.
    """
    for u in urls:
        tok = _SF.matches(u)
        if tok:
            return tok
    for host in _sf_candidate_hosts(urls)[:5]:
        if await _is_sf(host, fetcher):
            return f"{host}|{_domain_label(host)}"
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

    # Merge the top-1000 and mid-tier gap lists, dedup by name, highest filings first.
    sponsors: dict[str, dict] = {}
    for f in (GAP, MIDTIER):
        if f.exists():
            for s in json.loads(f.read_text()).get("uncovered_top", []):
                cur = sponsors.get(s["name"])
                if cur is None or s.get("filings", 0) > cur.get("filings", 0):
                    sponsors[s["name"]] = s
    todo = sorted(sponsors.values(), key=lambda s: -s.get("filings", 0))[:cap]
    seed_keys = load_seed_keys()
    print(f"SF census over {len(todo)} uncovered sponsors ...", flush=True)

    # Phase 1: Tavily + detect (concurrent).
    tokens: dict[int, str] = {}
    done = [0]

    async def find(idx: int, sponsor: dict, fetcher: AsyncFetcher) -> None:
        urls = await tavily(sponsor["name"], key, fetcher)
        tok = await detect(sponsor["name"], urls, fetcher)
        if tok:
            tokens[idx] = tok
        done[0] += 1
        if done[0] % 200 == 0:
            print(f"  scanned {done[0]}/{len(todo)} (SF hits so far: {len(tokens)})", flush=True)

    async with (
        AsyncFetcher(concurrency=16, per_host_rate=6, timeout=20.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, s in enumerate(todo):
            tg.start_soon(find, idx, s, fetcher)

    print(f"detected {len(tokens)} SF candidates; verifying live ...", flush=True)

    # Phase 2: verify each token live (limit=1) through the provider; keep live + unseeded.
    candidates: list[dict] = []
    taken: set[str] = set()

    async def verify(idx: int, token: str, fetcher: AsyncFetcher) -> None:
        siteid = token.split("|", 1)[1]
        if siteid in seed_keys or siteid in taken:
            return
        try:
            raws = await _SF.fetch(token, SearchQuery(limit=1), fetcher)
        except Exception:  # noqa: BLE001
            raws = []
        if raws:
            taken.add(siteid)
            candidates.append(
                {
                    "company": siteid,
                    "ats": "successfactors",
                    "token": token,
                    "domain": None,
                    "_sponsor": todo[idx]["name"],
                    "_filings": todo[idx].get("filings"),
                }
            )

    async with (
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=25.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, token in tokens.items():
            tg.start_soon(verify, idx, token, fetcher)

    candidates.sort(key=lambda c: -(c.get("_filings") or 0))
    for c in candidates:
        print(f"  + {c['_sponsor'][:32]:32} ({c.get('_filings') or 0:>5} filings) -> {c['token']}")
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"\nwrote {len(out)} SF candidates -> {shown}")
    print(f"next: .venv/bin/python scripts/build_registry.py {shown}")


if __name__ == "__main__":
    anyio.run(main)
