"""Fetch real postings (title + description head) for the seniority-level benchmark corpus.

Unlike the degree/yoe/skills corpora (which window the description), ``level.py`` is a TITLE-driven
classifier — it reads ``title`` first and falls back to a few high-confidence description phrases and
years-of-experience. So the corpus unit is a whole posting: the full ``title`` plus the head of the
description (where the seniority framing / "you'll be joining as a…" usually lives). Blind labeling
then assigns the seniority level per the labeling guide.

Sampled broadly across ATS so all rungs appear (intern → executive), with the natural heavy mass on
mid/senior/unknown. Deduped on title+company.

Output: unlabeled records -> scratchpad JSONL (id, source, company_key, title, description).

Usage:
    .venv/bin/python scripts/build_level_corpus.py [--per-ats 90] [--per-company 12] \
        [--max-total 900] --out <path.jsonl>
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

_DESC_HEAD = 1400


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
    per_ats = _arg("--per-ats", 90)
    per_company = _arg("--per-company", 12)
    max_total = _arg("--max-total", 900)
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "level_candidates.jsonl"

    load_builtins()
    companies = _sample_companies(per_ats)
    query = SearchQuery(limit=per_company)
    rows: list[dict] = []
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
            if not job.title:
                continue
            local.append(
                {
                    "id": job.id,
                    "source": ats,
                    "company_key": key,
                    "title": job.title.strip(),
                    "description": (job.description_text or "")[:_DESC_HEAD].strip(),
                }
            )
        if local:
            async with lock:
                rows.extend(local)

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for key, ats, token, domain in companies:
            tg.start_soon(grab, key, ats, token, domain, fetcher)

    # Dedup on title+company, cap total.
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        sig = f"{r['company_key']}::{r['title'].lower()}"
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(r)
        if len(deduped) >= max_total:
            break

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in deduped:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_src: dict[str, int] = {}
    for r in deduped:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print(f"wrote {len(deduped)} postings -> {out}")
    print(f"by source: {by_src}")
    print(f"distinct companies: {len({r['company_key'] for r in deduped})}")


if __name__ == "__main__":
    anyio.run(main)
