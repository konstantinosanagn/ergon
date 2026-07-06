"""Fetch real JD windows mentioning sponsorship for the sponsorship benchmark corpus.

``detect_sponsorship(text)`` only classifies postings that mention "sponsor" (everything else is
unknown), so this corpus anchors on the word "sponsor" (+ "visa"/"work authorization" context) to
harvest the postings that actually state a policy — both positive ("sponsorship available") and
negative ("we do not sponsor"). Windows are text[start-220 : end+220] around each cue, merged.

Output: unlabeled candidates -> scratchpad JSONL (id, source, company_key, title, text).

Usage:
    .venv/bin/python scripts/build_sponsorship_corpus.py [--per-ats 90] [--per-company 20] \
        [--max-total 500] --out <path.jsonl>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from ergon_tracker.registry.store import SeedRegistry  # noqa: E402

_CUE = re.compile(r"\bsponsor\w*", re.IGNORECASE)
_BACK, _FWD, _MAX = 220, 220, 700


def _windows(text: str) -> list[str]:
    spans: list[list[int]] = []
    for m in _CUE.finditer(text):
        lo, hi = max(0, m.start() - _BACK), min(len(text), m.end() + _FWD)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return [text[s:e][:_MAX].strip() for s, e in spans]


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
    per_company = _arg("--per-company", 20)
    max_total = _arg("--max-total", 500)
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "sponsorship_candidates.jsonl"

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
        except Exception:  # noqa: BLE001
            return
        local: list[dict] = []
        for raw in raws[:per_company]:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            desc = job.description_text
            if not desc or "sponsor" not in desc.lower():
                continue
            for win in _windows(desc)[:2]:  # cap per posting
                if len(win) < 40:
                    continue
                local.append({"id": job.id, "source": ats, "company_key": key, "title": job.title, "text": win})
        if local:
            async with lock:
                rows.extend(local)

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for key, ats, token, domain in companies:
            tg.start_soon(grab, key, ats, token, domain, fetcher)

    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        sig = r["text"][:160]
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
    print(f"wrote {len(deduped)} sponsorship windows -> {out}")
    print(f"by source: {by_src}")


if __name__ == "__main__":
    anyio.run(main)
