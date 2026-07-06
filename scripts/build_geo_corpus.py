"""Fetch real postings and collect DISTINCT location strings for the geo benchmark corpus.

``geo.py``'s ``normalize_geo(Location(raw=...))`` fills city/country/remote from the raw location
string, so the corpus unit is the location string itself — and we dedup on it (otherwise the sample
is 900x "Remote" / "San Francisco, CA"). We keep one example title/company per distinct raw string
for the labeler's context. Sampled broadly across ATS so US "City, ST", "City, Country", remote,
multi-location, and ATS-noise forms ("3 Locations", "US-Remote") all appear.

Output: unlabeled records -> scratchpad JSONL (location_raw, title, source, company_key).

Usage:
    .venv/bin/python scripts/build_geo_corpus.py [--per-ats 100] [--per-company 20] \
        [--max-total 800] --out <path.jsonl>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from ergon_tracker.registry.store import SeedRegistry  # noqa: E402


def _arg(flag: str, default: int) -> int:
    return int(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default


def _raw_of(job) -> str | None:
    if not job.locations:
        return None
    loc = job.locations[0]
    return (loc.raw or loc.as_text() or "").strip() or None


def _sample_companies(per_ats: int) -> list[tuple[str, str, str, str | None]]:
    by_ats: dict[str, list[tuple[str, str, str, str | None]]] = {}
    for key, entry in SeedRegistry().all().items():
        by_ats.setdefault(entry["ats"], []).append(
            (key, entry["ats"], entry["token"], entry.get("domain"))
        )
    picked: list[tuple[str, str, str, str | None]] = []
    for entries in by_ats.values():
        step = max(1, len(entries) // per_ats)
        picked.extend(entries[::step][:per_ats])
    return picked


async def main() -> None:
    per_ats = _arg("--per-ats", 100)
    per_company = _arg("--per-company", 20)
    max_total = _arg("--max-total", 800)
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "geo_candidates.jsonl"

    load_builtins()
    companies = _sample_companies(per_ats)
    query = SearchQuery(limit=per_company)
    by_raw: dict[str, dict] = {}
    lock = anyio.Lock()

    async def grab(key: str, ats: str, token: str, domain: str | None, fetcher: AsyncFetcher) -> None:
        provider = get_provider(ats)
        if provider is None:
            return
        try:
            raws = await provider.fetch(token, query, fetcher)
        except Exception:  # noqa: BLE001 - skip dead boards
            return
        local: list[dict] = []
        for raw in raws[:per_company]:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            lr = _raw_of(job)
            if not lr or len(lr) > 120:
                continue
            local.append({"location_raw": lr, "title": (job.title or "").strip(), "source": ats, "company_key": key})
        if local:
            async with lock:
                for r in local:
                    by_raw.setdefault(r["location_raw"], r)  # first example per distinct raw

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for key, ats, token, domain in companies:
            tg.start_soon(grab, key, ats, token, domain, fetcher)

    records = list(by_raw.values())[:max_total]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_src: dict[str, int] = {}
    for r in records:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print(f"wrote {len(records)} DISTINCT location strings -> {out}")
    print(f"by source: {by_src}")


if __name__ == "__main__":
    anyio.run(main)
