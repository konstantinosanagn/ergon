"""Ingest jobhive's public ATS-tenant CSVs -> candidates.json (for build_registry verify+merge).

jobhive (github kalil0321/ats-scrapers, MIT) maintains one CSV per ATS under
``ats-companies/{ats}.csv`` with columns ``name,slug,url`` — tens of thousands of real,
already-discovered tenants. Their slugs are *facts* (company name + public ATS slug), so we
don't brute-force-guess anything: we map each row directly to ergon_tracker's candidate schema and
let the EXISTING ``scripts/build_registry.py`` verify each board is live before merging. Their
data proposes; our verify gate disposes (and drops the stale ones, e.g. dead boards).

Only the 8 ATSes ergon_tracker has providers for are ingested (their other ~18 — join.com,
bamboohr, jazzhr, icims, teamtailor, … — need providers built first; see the roadmap).

Slug mapping
------------
* Simple ATSes (greenhouse/lever/ashby/smartrecruiters/workable/recruitee/personio): the
  candidate ``token`` is the row's ``slug`` (falling back to extracting it from ``url`` via the
  provider's own ``matches()`` when ``slug`` is absent).
* Workday: the ``slug`` alone is insufficient — our provider needs the composite
  ``tenant|wd|site``. We parse the row's ``url`` (a full careers URL) through
  ``WorkdayProvider.matches()`` to reconstruct it; rows we can't parse are dropped.

Attribution: source data © jobhive / kalil0321/ats-scrapers (MIT). Facts are not copyrightable;
we re-verify every entry independently before use.

Usage::

    .venv/bin/python scripts/ingest_jobhive_csvs.py [--ats greenhouse lever ...] [--limit N] [--out PATH]
    .venv/bin/python scripts/build_registry.py scripts/candidates_jobhive.json --dry-run
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from ergon_tracker.providers.workday import WorkdayProvider  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_jobhive.json"

# jobhive raw CSV base (one file per ATS).
_RAW = "https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/ats-companies/{ats}.csv"

# ATSes ergon_tracker has providers for (their CSV stem == our provider name for all 8).
SUPPORTED_ATSES = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "workable",
    "recruitee",
    "personio",
    "bamboohr",
    "breezy",
    "teamtailor",
    "join",
    "rippling",
    "pinpoint",
    "jazzhr",  # public app.jazz.co syndication feed (keyless); ~85% of jobhive's tenants verify live
)

# Where our provider name differs from jobhive's CSV stem.
CSV_STEM = {"join": "join_com"}

_KEY_RE = re.compile(r"[^a-z0-9]+")


def company_key(name: str) -> str:
    """Slugify a company name into a registry key (lowercase, non-alnum runs -> single '-')."""
    return _KEY_RE.sub("-", name.strip().lower()).strip("-")


# --- pure row -> candidate mapping (no network; unit-tested) -----------------------------------


def row_to_candidate(ats: str, row: dict[str, str]) -> dict[str, object] | None:
    """Map one jobhive CSV row to a ergon_tracker candidate dict, or ``None`` if unmappable.

    Uses ``slug`` when present; otherwise recovers the token from ``url`` via the provider's
    ``matches()``. Workday always parses ``url`` into the ``tenant|wd|site`` composite.
    """
    name = (row.get("name") or row.get("slug") or "").strip()
    slug = (row.get("slug") or "").strip()
    url = (row.get("url") or "").strip()
    if not (name or slug):
        return None
    key = company_key(name or slug)
    if not key:
        return None

    if ats == "workday":
        token = WorkdayProvider.matches(url) if url else None
        if not token:
            return None
        tenant, wd, site = token.split("|")
        return {"company": key, "ats": "workday", "tenant": tenant, "wd": wd, "site": site,
                "domain": None}

    # Simple ATSes: prefer the slug; fall back to extracting it from the url via the provider.
    token = slug
    if not token and url:
        load_builtins()  # registry must be populated before get_provider() in this fallback
        provider = get_provider(ats)
        token = provider.matches(url) if provider else None
    if not token:
        return None
    return {"company": key, "ats": ats, "token": token, "domain": None}


def parse_jobhive_csv(text: str, ats: str) -> tuple[list[dict[str, object]], int]:
    """Parse one jobhive CSV into candidates; return ``(candidates, skipped_unmappable)``.

    Deduplicates by company key within the file. Tolerates the legacy two-column ``name,url``
    shape (no ``slug``) by falling back to url extraction.
    """
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    skipped = 0
    for row in reader:
        cand = row_to_candidate(ats, {k: (v or "") for k, v in row.items()})
        if cand is None:
            skipped += 1
            continue
        key = str(cand["company"])
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out, skipped


def load_seed_keys(seed_path: Path = SEED) -> set[str]:
    if not seed_path.exists():
        return set()
    return set(json.loads(seed_path.read_text()).get("companies", {}))


# --- network fetch -----------------------------------------------------------------------------


async def fetch_csv(ats: str, fetcher: AsyncFetcher) -> str | None:
    try:
        return await fetcher.get_text(_RAW.format(ats=CSV_STEM.get(ats, ats)))
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"  [{ats}] fetch failed: {type(exc).__name__}: {exc}")
        return None


async def ingest(atses: list[str], fetcher: AsyncFetcher, limit: int | None) -> list[dict]:
    load_builtins()
    seed_keys = load_seed_keys()
    all_candidates: list[dict] = []
    global_seen: set[str] = set()

    for ats in atses:
        text = await fetch_csv(ats, fetcher)
        if text is None:
            continue
        cands, skipped = parse_jobhive_csv(text, ats)
        new = [c for c in cands if str(c["company"]) not in seed_keys
               and str(c["company"]) not in global_seen]
        for c in new:
            global_seen.add(str(c["company"]))
        if limit is not None:
            new = new[:limit]
        all_candidates.extend(new)
        print(f"  [{ats}] rows->candidates={len(cands)} unmappable={skipped} "
              f"new(after seed/dedupe)={len(new)}")
    return all_candidates


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    limit: int | None = None
    atses: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out_path = Path(args[i + 1]); i += 2
        elif a == "--limit":
            limit = int(args[i + 1]); i += 2
        elif a == "--ats":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                atses.append(args[i]); i += 1
        else:
            print(f"unknown flag: {a}"); return

    if not atses:
        atses = list(SUPPORTED_ATSES)
    unknown = [a for a in atses if a not in SUPPORTED_ATSES]
    if unknown:
        print(f"unsupported ATS(es): {unknown}; supported: {list(SUPPORTED_ATSES)}")
        return

    print(f"ingesting jobhive CSVs for: {atses}  (limit={limit})")
    async with AsyncFetcher(concurrency=8, per_host_rate=8, timeout=60.0) as fetcher:
        candidates = await ingest(atses, fetcher, limit)

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
