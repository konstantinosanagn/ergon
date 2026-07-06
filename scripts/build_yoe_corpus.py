"""Fetch real JDs and extract years-of-experience windows for the yoe benchmark corpus.

Sibling of ``scripts/build_degree_corpus.py`` (same fetch/window/dedup machinery), retargeted to
years-of-experience. The candidate net is deliberately WIDER than ``yoe.py``: it anchors on ANY
``<number> year|yr|yoe|month`` mention (digits or spelled-out one..ninety) so the benchmark can see
the extractor's true recall gaps AND surface its FP traps — vesting/cliff schedules, company age
("25 years in business"), calendar spans ("over the last 5 years"), ages ("18 years or older"),
"N years ago", and contract/leave lengths ("6 month contract"). Locating candidates with a broad net
(not yoe.py's own rules) is what keeps the recall number honest.

Windows are ``text[start-450 : end+250]`` around each hit — enough sentence context for a human to
judge whether the number is a required experience quantity. Overlaps merged, capped per company.

Output: unlabeled candidates -> scratchpad JSONL (id, source, company_key, title, text). Labeling is
a separate blind step.

Usage:
    .venv/bin/python scripts/build_yoe_corpus.py [--per-ats 70] [--per-company 15] \
        [--max-per-company 3] [--max-total 800] --out <path.jsonl>
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

_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty"
)
# Broad net: a number (digit or word) adjacent to a year/experience unit, plus bare "YOE". Anchors
# on the quantity so both real requirements and FP traps (vesting/age/business/ago/contract) surface.
_YOE_NET = re.compile(
    rf"\b(?:\d{{1,2}}|{_WORDS})\s*\+?\s*(?:years?|yrs?|yoe|months?)\b|\byoe\b",
    re.IGNORECASE,
)

_BACK = 450
_FWD = 250
_MAX_WIN = 1100


def _windows(text: str) -> list[str]:
    spans: list[list[int]] = []
    for m in _YOE_NET.finditer(text):
        lo, hi = max(0, m.start() - _BACK), min(len(text), m.end() + _FWD)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return [text[s:e][:_MAX_WIN].strip() for s, e in spans]


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
    per_ats = _arg("--per-ats", 70)
    per_company = _arg("--per-company", 15)
    max_per_company = _arg("--max-per-company", 3)
    max_total = _arg("--max-total", 800)
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "yoe_candidates.jsonl"

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
        n_win = 0
        for raw in raws[:per_company]:
            if n_win >= max_per_company:
                break
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            desc = job.description_text
            if not desc:
                continue
            for win in _windows(desc):
                if len(win) < 40:
                    continue
                local.append(
                    {"id": job.id, "source": ats, "company_key": key, "title": job.title, "text": win}
                )
                n_win += 1
                if n_win >= max_per_company:
                    break
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
        sig = r["text"][:200]
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
    print(f"wrote {len(deduped)} candidate windows -> {out}")
    print(f"by source: {by_src}")
    print(f"distinct companies: {len({r['company_key'] for r in deduped})}")


if __name__ == "__main__":
    anyio.run(main)
