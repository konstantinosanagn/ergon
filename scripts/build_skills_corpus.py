"""Fetch real JDs and extract requirement/skill-section windows for the skills benchmark corpus.

Sibling of build_degree/yoe_corpus.py. skills.py is SET-valued (returns the set of canonical skills
in a text), so the corpus anchors on **skill-CONTEXT cues** ("proficient in", "experience with",
"tech stack", "technologies", "familiar with", "programming languages") rather than on the gazetteer's
own surface forms — that way a window can contain skills the gazetteer DOESN'T know yet, so blind
labeling surfaces true recall gaps (skills to ADD), not just what the extractor already finds. FP
traps (R&D, go-to-market, "the big picture") ride along naturally in these sections.

Windows are text[start-300 : end+320] around each cue (a requirements bullet/list is usually right
there), merged, capped per company.

Output: unlabeled candidates -> scratchpad JSONL (id, source, company_key, title, text).

Usage:
    .venv/bin/python scripts/build_skills_corpus.py [--per-ats 70] [--per-company 15] \
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

_SKILL_CTX = re.compile(
    r"\b(?:proficien\w*|experience\s+with|familiar\s+with|knowledge\s+of|expertise\s+in|"
    r"skills?|tech(?:nical)?\s*stack|technologies|tooling|programming\s+languages?|"
    r"hands[\s-]on\s+with|working\s+knowledge|fluent\s+in|competen\w*|proficiency)\b",
    re.IGNORECASE,
)

_BACK = 300
_FWD = 320
_MAX_WIN = 1200


def _windows(text: str) -> list[str]:
    spans: list[list[int]] = []
    for m in _SKILL_CTX.finditer(text):
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
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "skills_candidates.jsonl"

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
                if len(win) < 60:
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
