"""Harvest ATS board tokens from the apply URLs of aggregator-sourced jobs -> candidates.json.

Aggregators (RemoteOK, Adzuna, Themuse, Remotive, ...) re-list jobs whose apply/listing URL is
frequently the employer's OWN ATS board (``boards.greenhouse.io/{token}``,
``jobs.lever.co/{token}``, ``jobs.ashbyhq.com/{token}``, ...). We already fetch those feeds, so
mining their apply URLs is near-zero-cost discovery: pull each aggregator's jobs, look at every
apply/listing URL, recover ``(ats, token)`` via each provider's ``matches()`` — only a real board
matches, so there are no false positives — filter against the current registry, and let
``build_registry`` verify + merge what's new.

Usage::

    .venv/bin/python scripts/harvest_aggregator_apply_urls.py [--limit-per N] [--out PATH]
    .venv/bin/python scripts/build_registry.py scripts/candidates_apply_urls.json --gentle --onboard-empty
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ingest_jobhive_csvs import company_key, load_seed_keys  # noqa: E402

from ergon.engine import AGGREGATOR_PROVIDERS  # noqa: E402
from ergon.http import AsyncFetcher  # noqa: E402
from ergon.models import SearchQuery  # noqa: E402
from ergon.providers.base import get_provider, iter_providers, load_builtins  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_apply_urls.json"


def resolve_ats_url(url: str) -> tuple[str, str] | None:
    """Recover ``(ats, token)`` from a URL via providers' ``matches()``; None if no ATS claims it.

    Aggregator providers return None from ``matches()`` (they never claim a URL), so their own
    listing links can't be mistaken for boards — only genuine ATS board URLs resolve.
    """
    if not url:
        return None
    for provider in iter_providers():
        token = provider.matches(url)
        if token:
            return provider.name, token
    return None


def urls_to_candidates(
    pairs: list[tuple[str, str]], seed_keys: set[str]
) -> list[dict[str, object]]:
    """Map ``(company_name, url)`` pairs to candidate dicts, skipping non-ATS URLs, companies
    already in the registry, and intra-batch duplicates."""
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for company, url in pairs:
        res = resolve_ats_url(url)
        if res is None:
            continue
        ats, token = res
        key = company_key(company or token)
        if not key or key in seed_keys or key in seen:
            continue
        seen.add(key)
        out.append({"company": key, "ats": ats, "token": token, "domain": None})
    return out


async def collect_pairs(
    providers: list[str], limit_per: int, fetcher: AsyncFetcher
) -> list[tuple[str, str]]:
    """Fetch each aggregator's jobs and return (company_name, candidate_url) pairs from each
    job's normalized apply_url and its raw listing url."""
    pairs: list[tuple[str, str]] = []
    for name in providers:
        prov = get_provider(name)
        if prov is None:
            continue
        try:
            raws = await prov.fetch("", SearchQuery(limit=limit_per), fetcher)
        except Exception as exc:  # noqa: BLE001 - one aggregator down never sinks the harvest
            print(f"  [{name}] fetch failed: {type(exc).__name__}: {exc}")
            continue
        for raw in raws:
            try:
                job = prov.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            company = job.company or raw.company or ""
            for url in (job.apply_url, raw.url):
                if url:
                    pairs.append((company, url))
        print(f"  [{name}] raws={len(raws)}")
    return pairs


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    limit_per = 200
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--limit-per":
            limit_per = int(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    load_builtins()
    seed_keys = load_seed_keys()
    providers = sorted(AGGREGATOR_PROVIDERS)
    print(f"mining apply URLs from aggregators: {providers} (limit_per={limit_per})")
    async with AsyncFetcher(concurrency=8, per_host_rate=4, timeout=30.0) as fetcher:
        pairs = await collect_pairs(providers, limit_per, fetcher)
    candidates = urls_to_candidates(pairs, seed_keys)
    by_ats: dict[str, int] = {}
    for c in candidates:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"\n{len(pairs)} apply URLs -> {len(candidates)} new ATS candidates  by_ats={by_ats}")
    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    rel = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
    print(f"wrote {rel}")
    print(f"next: .venv/bin/python scripts/build_registry.py {rel} --gentle --onboard-empty")


if __name__ == "__main__":
    anyio.run(main)
