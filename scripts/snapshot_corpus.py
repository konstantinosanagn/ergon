"""Snapshot a real-job corpus for extraction evaluation.

Fetches a stratified sample of live postings across all four ATS providers and writes the
extractor *inputs* (pre-enrichment) to data/corpus.jsonl — one JSON object per posting:

    {id, source, company_key, title, description_text, location_raw, structured_salary}

A stratified sample of this corpus is then hand-labeled by agents into tests/data/gold.jsonl.

Usage:
    .venv/bin/python scripts/snapshot_corpus.py [--per-ats 25] [--per-company 40]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.extract.windows import cue_windows  # noqa: E402
from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from ergon_tracker.registry.store import SeedRegistry  # noqa: E402

OUT = ROOT / "data" / "corpus.jsonl"


def _arg(flag: str, default: int) -> int:
    if flag in sys.argv:
        return int(sys.argv[sys.argv.index(flag) + 1])
    return default


def _sample_companies(per_ats: int) -> list[tuple[str, str, str, str | None]]:
    """Return (company_key, ats, token, domain), up to per_ats per ATS, spread across the seed."""
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
    per_ats = _arg("--per-ats", 25)
    per_company = _arg("--per-company", 40)
    load_builtins()
    companies = _sample_companies(per_ats)
    query = SearchQuery(limit=per_company)
    rows: list[dict] = []
    lock = anyio.Lock()

    async def grab(
        key: str, ats: str, token: str, domain: str | None, fetcher: AsyncFetcher
    ) -> None:
        provider = get_provider(ats)
        if provider is None:
            return
        try:
            raws = await provider.fetch(token, query, fetcher)
        except Exception:  # noqa: BLE001 - skip dead boards in a snapshot
            return
        local: list[dict] = []
        for raw in raws[:per_company]:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            sal = None
            if job.salary and (job.salary.min_amount or job.salary.max_amount):
                sal = job.salary.model_dump()
            local.append(
                {
                    "id": job.id,
                    "source": ats,
                    "company_key": key,
                    "title": job.title,
                    # store cue-anchored windows, not the full description: compact + keeps the
                    # comp/yoe signal that head-truncation would drop (it lives deep in the JD).
                    "description_text": cue_windows(job.description_text),
                    "location_raw": (job.locations[0].raw if job.locations else None),
                    "structured_salary": sal,
                }
            )
        async with lock:
            rows.extend(local)

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for key, ats, token, domain in companies:
            tg.start_soon(grab, key, ats, token, domain, fetcher)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_src: dict[str, int] = {}
    with_desc = 0
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        if r["description_text"]:
            with_desc += 1
    print(f"wrote {len(rows)} postings -> {OUT.relative_to(ROOT)}")
    print(f"by source: {by_src}")
    print(f"with description_text: {with_desc}/{len(rows)}")


if __name__ == "__main__":
    anyio.run(main)
