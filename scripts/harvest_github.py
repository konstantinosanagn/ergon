"""Harvest ATS board tokens from GitHub code search -> candidates.json.

Countless repos hardcode ATS board URLs in config files, job-board aggregators, and scrapers:
``boards.greenhouse.io/{token}``, ``jobs.ashbyhq.com/{token}``, ``{token}.pinpointhq.com``, etc.
GitHub's code-search index has already "seen" these across millions of repos, so searching it
for each ATS host and pulling the matching text fragments yields real, in-the-wild tokens — a
high-signal source that's completely independent of Common Crawl (and unaffected by its
outages).

We reuse the Common Crawl harvester's per-ATS token EXTRACTORS (same URL->token logic), so the
two sources stay consistent. Output is a ``candidates.json`` that ``build_registry.py`` verifies
live before merging. Propose here; verify there.

Auth: GitHub code search requires a token. Set ``GITHUB_TOKEN`` (or ``GH_TOKEN``); without it
the script prints a setup hint and exits (graceful, like the env-configured providers).

Usage::

    GITHUB_TOKEN=ghp_... .venv/bin/python scripts/harvest_github.py [greenhouse ashby ...] [--pages N]
    .venv/bin/python scripts/build_registry.py scripts/candidates_github.json --dry-run
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import CONFIGS, load_seed_keys  # noqa: E402  (reuse extractors + seed)

from ergon_tracker.http import AsyncFetcher  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_github.json"

_SEARCH = "https://api.github.com/search/code"
# Pull any URL out of a code-search text fragment; the per-ATS extractor decides if it's a board.
_URL_RE = re.compile(r"https?://[^\s\"'`<>)\\]+", re.IGNORECASE)

# GitHub code search supports path-/host-based ATSes well (the token is literally in the URL
# string in source files). Subdomain ATSes also work since the host appears in the URL.
DEFAULT_ATSES = ("greenhouse", "ashby", "workable", "lever", "rippling", "pinpoint")


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


# --- pure extraction (no network; unit-tested) ------------------------------------------------


def tokens_from_fragments(ats: str, fragments: list[str]) -> list[str]:
    """Extract unique board tokens for one ATS from code-search text fragments.

    Finds every URL in each fragment and runs the ATS's Common Crawl extractor over it, so the
    token rules (junk filtering, case handling, subdomain vs path) match exactly.
    """
    source = CONFIGS[ats]
    seen: set[str] = set()
    out: list[str] = []
    for frag in fragments:
        for url in _URL_RE.findall(frag or ""):
            tok = source.extract(url)  # type: ignore[operator]
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def fragments_from_search(payload: dict) -> list[str]:
    """Pull all ``text_matches[].fragment`` strings from a GitHub code-search response."""
    frags: list[str] = []
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        for tm in item.get("text_matches") or []:
            frag = tm.get("fragment")
            if isinstance(frag, str) and frag:
                frags.append(frag)
    return frags


# --- network harvest --------------------------------------------------------------------------


async def _search_page(ats: str, fetcher: AsyncFetcher, token: str, page: int) -> list[str]:
    """One page of GitHub code search for an ATS host; return text fragments ([] on failure)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.text-match+json",  # include matching text fragments
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {"q": CONFIGS[ats].query, "per_page": "100", "page": str(page)}
    try:
        data = await fetcher.get_json(_SEARCH, params=params, headers=headers)
        return fragments_from_search(data)
    except Exception as exc:  # noqa: BLE001 - report and continue (secondary rate limits etc.)
        print(f"  [{ats}] page {page} failed: {type(exc).__name__}: {str(exc)[:60]}")
        return []


async def harvest(atses: list[str], fetcher: AsyncFetcher, token: str, pages: int,
                  limit: int) -> list[dict[str, object]]:
    seed_keys = load_seed_keys()
    candidates: list[dict[str, object]] = []
    global_seen: set[str] = set()

    for ats in atses:
        frags: list[str] = []
        # GitHub caps code search at 1000 results (10 pages x 100). Pages are fetched
        # sequentially to respect the strict code-search secondary rate limit.
        for page in range(1, min(pages, 10) + 1):
            page_frags = await _search_page(ats, fetcher, token, page)
            frags.extend(page_frags)
            if not page_frags:
                break  # no more results
        tokens = tokens_from_fragments(ats, frags)
        new = [t for t in tokens if t not in seed_keys and t not in global_seen][:limit]
        for t in new:
            global_seen.add(t)
            candidates.append({"company": t, "ats": ats, "token": t, "domain": None})
        print(f"  [{ats}] fragments={len(frags)} tokens={len(tokens)} new={len(new)}")
    return candidates


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    pages = 10
    limit = 100000
    atses: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif a == "--pages":
            pages = int(args[i + 1])
            i += 2
        elif a == "--limit":
            limit = int(args[i + 1])
            i += 2
        elif a.startswith("--"):
            print(f"unknown flag: {a}")
            return
        else:
            atses.append(a)
            i += 1

    token = _token()
    if not token:
        print("GITHUB_TOKEN (or GH_TOKEN) not set — GitHub code search requires auth.")
        print("Create one at https://github.com/settings/tokens (public_repo scope is enough).")
        return

    if not atses:
        atses = list(DEFAULT_ATSES)
    unknown = [a for a in atses if a not in CONFIGS]
    if unknown:
        print(f"unknown ATS(es): {unknown}; known: {sorted(CONFIGS)}")
        return

    print(f"harvesting GitHub code search for: {atses}  (pages/ats<= {min(pages, 10)})")
    # Code search secondary rate limit is strict (~10/min); keep per-host pace gentle.
    async with AsyncFetcher(concurrency=2, per_host_rate=1, timeout=60.0) as fetcher:
        candidates = await harvest(atses, fetcher, token, pages, limit)

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
    anyio.run(main)
