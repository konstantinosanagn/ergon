"""Upgrade existing registry entries from the weak ``adzuna`` aggregator fallback to a giant's
AUTHORITATIVE own-ATS (Workday/Oracle/Phenom/SuccessFactors/…).

``build_registry.py`` only ADDS new company keys — it never replaces an existing entry (so a
giant captured via the lowest-priority Adzuna fallback stays there even once its real board is
found). This tool does the in-place upgrade, but SAFELY:

* resolves the canonical ``_core`` company key (so we patch the real giant, not a suffix variant);
* re-fetches the candidate token LIVE and refuses to write unless it returns >= MIN_JOBS;
* only overwrites when the new ATS outranks the current one (ATS_PRIORITY), or the key is new.

Input: a JSON list of ``{"company": "<giant name OR _core key>", "ats": "...", "token": "...",
"domain": "..."}``. Run:

    .venv/bin/python scripts/upgrade_registry.py scripts/candidates_upgrade.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import anyio  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from build_registry import ATS_PRIORITY  # noqa: E402
from harvest_tokens import _core  # noqa: E402

SEED = ROOT / "src/ergon_tracker/registry/data/seed.json"
MIN_JOBS = 1


async def _live_count(ats: str, token: str, fetcher: AsyncFetcher) -> int:
    try:
        prov = get_provider(ats)
        raws = await prov.fetch(token, SearchQuery(limit=80), fetcher)
        return len(raws)
    except Exception:
        return 0


async def main() -> None:
    load_builtins()
    cands = json.loads(Path(sys.argv[1]).read_text())
    seed = json.loads(SEED.read_text())
    companies = seed["companies"]

    upgraded = skipped = 0
    async with AsyncFetcher() as fetcher:
        for c in cands:
            key = _core(c["company"]) if " " in c["company"] else c["company"].lower()
            ats, token = c["ats"], c["token"]
            cnt = await _live_count(ats, token, fetcher)
            cur = companies.get(key)
            cur_ats = cur["ats"] if cur else None
            if cnt < MIN_JOBS:
                print(f"  SKIP {key:34s} {ats} live={cnt} (no jobs)")
                skipped += 1
                continue
            # Only overwrite when strictly better priority (lower number) than the current ATS.
            if cur and ATS_PRIORITY.get(ats, 99) >= ATS_PRIORITY.get(cur_ats, 99) and cur_ats != ats:
                print(f"  SKIP {key:34s} {ats}({cnt}) NOT better than {cur_ats}")
                skipped += 1
                continue
            companies[key] = {"ats": ats, "token": token, "domain": c.get("domain")}
            print(f"  UPGRADE {key:30s} {cur_ats or '(new)'} -> {ats}  live={cnt}")
            upgraded += 1

    SEED.write_text(json.dumps(seed, indent=1))
    print(f"\nupgraded={upgraded} skipped={skipped} total={len(companies)}")


if __name__ == "__main__":
    anyio.run(main)
