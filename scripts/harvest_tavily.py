"""Harvest ATS board tokens via Tavily Search (fresh web index) -> candidates.json.

Tavily's search index is CURRENT, so unlike Common Crawl (stale monthly snapshots that gave a
77% dead rate on an 18-month-old crawl), host-restricted searches surface boards that are LIVE
RIGHT NOW. ``include_domains`` pins each query to a single ATS host, so every result is a board
URL; we run many role/function queries to widen coverage past the 20-results-per-query cap, then
extract tokens (reusing harvest_commoncrawl's per-ATS extractors) and feed the live verify gate.

Why not Map/Crawl: both tree-expand from a root URL via internal links, which for a multi-tenant
host (boards.greenhouse.io) only reaches the *vendor's own* marketing pages, never tenant boards
— verified empirically. Search is the right primitive for tenant discovery.

Bonus: Tavily can surface boards Common Crawl can't — e.g. lever, whose board paths are
robots-blocked from bulk crawling but are still in a search index.

Auth: ``TAVILY_API_KEY`` from .env or environment; graceful skip without.

Usage::

    .venv/bin/python scripts/harvest_tavily.py [greenhouse lever ...] [--max-queries N] [--recent]
    .venv/bin/python scripts/build_registry.py scripts/candidates_tavily.json --dry-run
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import CONFIGS, CCSource, load_seed_keys  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_tavily.json"
_API = "https://api.tavily.com/search"

# Path/host ATSes search best (token is in the URL). lever is included deliberately: CC can't
# reach it (robots-blocked) but a search index can.
DEFAULT_ATSES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "rippling")

# Broad function/role terms — each host-restricted query returns up to 20 distinct board URLs,
# so variety (not depth) is how we widen coverage.
DEFAULT_QUERIES = [
    # Engineering
    "software engineer", "senior software engineer", "staff software engineer",
    "frontend engineer", "backend engineer", "full stack engineer", "mobile engineer",
    "ios engineer", "android engineer", "devops engineer", "site reliability engineer",
    "platform engineer", "security engineer", "data engineer", "machine learning engineer",
    "ai engineer", "qa engineer", "embedded engineer", "engineering manager",
    # Data & product
    "data scientist", "data analyst", "product manager", "senior product manager",
    "product designer", "ux designer", "ux researcher", "technical program manager",
    # Go-to-market
    "account executive", "sales development representative", "sales manager",
    "marketing manager", "growth marketing", "content marketing", "demand generation",
    "customer success manager", "solutions engineer", "partnerships manager",
    # G&A
    "recruiter", "technical recruiter", "people operations", "hr business partner",
    "finance manager", "accountant", "controller", "operations manager", "legal counsel",
    # Other / seniority
    "research scientist", "business analyst", "project manager", "customer support",
    "community manager", "internship", "new grad", "director", "vice president",
]

# Orthogonal query dimensions (engaged with --variants): a role term returns the most
# *prominent* boards for that role; pairing it with an industry or location surfaces a different
# slice of companies, which is how we widen past the 20-results-per-query cap.
INDUSTRY_QUERIES = [
    "fintech", "healthcare", "biotech", "pharma", "gaming", "crypto", "web3", "cybersecurity",
    "robotics", "climate tech", "clean energy", "ecommerce", "logistics", "supply chain",
    "edtech", "legal tech", "real estate tech", "insurtech", "manufacturing", "media",
    "advertising", "consulting", "hardware", "semiconductor", "aerospace", "defense",
    "automotive", "agriculture", "hospitality", "telecom", "ai startup", "developer tools",
]
LOCATION_QUERIES = [
    "San Francisco", "New York", "London", "Berlin", "Austin", "Seattle", "Boston", "Toronto",
    "Amsterdam", "Paris", "remote", "Bangalore", "Singapore", "Tel Aviv", "Dublin", "Munich",
    "Stockholm", "Sydney", "Chicago", "Los Angeles", "Denver", "Miami", "Vancouver", "Tokyo",
]


def load_key() -> str | None:
    """Read TAVILY_API_KEY from the environment, falling back to the repo .env file."""
    key = os.environ.get("TAVILY_API_KEY")
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TAVILY_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# --- pure extraction (no network; unit-tested) ------------------------------------------------


def tokens_from_results(source: CCSource, results: list[dict]) -> list[str]:
    """Extract unique board tokens from Tavily result objects via the ATS's CC extractor."""
    seen: set[str] = set()
    out: list[str] = []
    for r in results:
        url = r.get("url") if isinstance(r, dict) else None
        tok = source.extract(url) if url else None  # type: ignore[operator]
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# --- network harvest --------------------------------------------------------------------------


def search(client: httpx.Client, source: CCSource, query: str, key: str,
           recent: bool) -> list[dict]:
    """One host-restricted Tavily search; returns the raw result objects ([] on failure)."""
    body: dict[str, object] = {
        "query": query,
        "include_domains": [source.query],  # pin results to this ATS host/domain
        "max_results": 20,
        "search_depth": "basic",
    }
    if recent:
        body["time_range"] = "year"
    try:
        r = client.post(_API, headers={"Authorization": f"Bearer {key}"}, json=body)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"    [{source.ats}] query {query!r} failed: {type(exc).__name__}: {str(exc)[:50]}")
        return []


def harvest(atses: list[str], queries: list[str], key: str, recent: bool) -> list[dict]:
    seed_keys = load_seed_keys()
    candidates: list[dict[str, object]] = []
    global_seen: set[str] = set()

    with httpx.Client(timeout=60.0) as client:
        for name in atses:
            source = CONFIGS[name]
            tokens: list[str] = []
            for q in queries:
                tokens.extend(tokens_from_results(source, search(client, source, q, key, recent)))
                time.sleep(0.2)  # light pacing to stay under Tavily's secondary rate limit
            uniq = list(dict.fromkeys(tokens))  # de-dupe, keep order
            new = [t for t in uniq if t not in seed_keys and t not in global_seen]
            for t in new:
                global_seen.add(t)
                candidates.append({"company": t, "ats": name, "token": t, "domain": None})
            print(f"  [{name}] queries={len(queries)} tokens={len(uniq)} new={len(new)}")
    return candidates


def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    max_queries: int | None = None
    recent = False
    variants = False
    atses: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif a == "--max-queries":
            max_queries = int(args[i + 1])
            i += 2
        elif a == "--recent":
            recent = True
            i += 1
        elif a == "--variants":
            variants = True
            i += 1
        elif a.startswith("--"):
            print(f"unknown flag: {a}")
            return
        else:
            atses.append(a)
            i += 1

    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set (env or .env). Add it and retry.")
        return
    if not atses:
        atses = list(DEFAULT_ATSES)
    unknown = [a for a in atses if a not in CONFIGS]
    if unknown:
        print(f"unknown ATS(es): {unknown}; known: {sorted(CONFIGS)}")
        return
    queries = list(DEFAULT_QUERIES)
    if variants:
        queries += INDUSTRY_QUERIES + LOCATION_QUERIES
    if max_queries:
        queries = queries[:max_queries]

    print(f"harvesting Tavily search for {atses} x {len(queries)} queries (recent={recent})")
    candidates = harvest(atses, queries, key, recent)

    by_ats: dict[str, int] = {}
    for c in candidates:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"\ntotal new candidates: {len(candidates)}  by_ats={by_ats}")
    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"wrote {shown}")
    print(f"\nnext: .venv/bin/python scripts/build_registry.py {shown} --dry-run")


if __name__ == "__main__":
    main()
