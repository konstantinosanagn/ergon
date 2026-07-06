"""Collect DISTINCT companies (name + domain) for the sector (industry) benchmark corpus.

``sector`` is a COMPANY-level classification (name-keyword rules + a company->sector gazetteer), so
the corpus unit is a company, deduped. We keep one example job title per company for the labeler's
context. Blind labeling then assigns the company's industry from its name/domain; comparing to the
extractor measures both accuracy (when it returns a sector) and coverage (how often it returns None).

Output: unlabeled records -> scratchpad JSONL (company_key, company, domain, source, example_title).

Usage:
    .venv/bin/python scripts/build_sector_corpus.py [--per-ats 130] [--per-company 4] \
        [--max-total 700] --out <path.jsonl>
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
    per_ats = _arg("--per-ats", 130)
    per_company = _arg("--per-company", 4)
    max_total = _arg("--max-total", 700)
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "sector_candidates.jsonl"

    load_builtins()
    companies = _sample_companies(per_ats)
    query = SearchQuery(limit=per_company)
    by_company: dict[str, dict] = {}
    lock = anyio.Lock()

    async def grab(key: str, ats: str, token: str, domain: str | None, fetcher: AsyncFetcher) -> None:
        provider = get_provider(ats)
        if provider is None:
            return
        try:
            raws = await provider.fetch(token, query, fetcher)
        except Exception:  # noqa: BLE001
            return
        got = None
        for raw in raws[:per_company]:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            name = (job.company or "").strip()
            if not name:
                continue
            got = {
                "company_key": key,
                "company": name,
                "domain": (job.company_domain or domain or None),
                "source": ats,
                "example_title": (job.title or "").strip()[:70],
            }
            break
        if got:
            async with lock:
                by_company.setdefault(got["company"].lower(), got)

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for key, ats, token, domain in companies:
            tg.start_soon(grab, key, ats, token, domain, fetcher)

    records = list(by_company.values())[:max_total]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} distinct companies -> {out}")
    print(f"with domain: {sum(1 for r in records if r['domain'])}/{len(records)}")


if __name__ == "__main__":
    anyio.run(main)
