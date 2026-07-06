"""Fetch real JDs and extract degree-context windows for the degree benchmark corpus.

Mirrors ``scripts/snapshot_corpus.py`` but is degree-specific and, crucially, extracts candidate
windows with a **broad education net that is deliberately WIDER than ``degree.py``'s own patterns**.
This is what makes the resulting benchmark honest: if we located candidates using the extractor's
own gazetteer we could never measure a recall gap in that gazetteer. The net anchors on any
education-ish token (degree names, dotted/dot-less abbreviations, GED, high-school, PharmD/DVM/JD,
"scrum master", bare "MS"/"MA") so that a window a human labels as "requires a bachelor's" which
``degree.py`` happens to miss will surface as a scored miss — and FP traps ("MS Office",
"high degree of autonomy", "Boston, MA", "360 degree") surface as negatives.

Windows preserve the governing section header: each is ``text[start-650 : end+380]``, which is >=
the extractor's own ``_SECTION_WINDOW`` (600 back) + ``_SEGMENT_CAP`` (300), so running the
extractor on the cropped window yields exactly what it would on the full JD. Overlapping windows
in one JD are merged; windows are capped per company for cross-company diversity.

Output: unlabeled candidates -> scratchpad JSONL (id, source, company_key, title, text). Labeling
is a separate, blind step (see the degree corpus plan).

Usage:
    .venv/bin/python scripts/build_degree_corpus.py [--per-ats 60] [--per-company 15] \
        [--max-per-company 3] [--max-total 700] --out <path.jsonl>
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

# Broad education net — intentionally a SUPERSET of degree.py's final patterns (see module docstring).
# Standalone "education"/"qualifications"/"experience" are NOT anchors (they flood every JD); the
# window radius pulls in that surrounding section-header context anyway.
_EDU_NET = re.compile(
    r"\b("
    r"degrees?|diplomas?|bachelor(?:'|’)?s?|master(?:'|’)?s?|baccalaureate|"
    r"doctora(?:te|l)|ph\.?\s?d|associate(?:'|’)?s?|undergraduate|post[-\s]?graduate|graduate|"
    r"b\.?s\.?|b\.?a\.?|b\.?eng|m\.?s\.?|m\.?a\.?|m\.?eng|mba|bsc|msc|ged|"
    r"high\s?school|(?:4|four)[-\s]year|pharm\.?\s?d|d\.?v\.?m|j\.?d|juris\s+doctor|scrum\s+master"
    r")\b",
    re.IGNORECASE,
)

_BACK = 650
_FWD = 380
_MAX_WIN = 1500


def _windows(text: str) -> list[str]:
    """Merged education-context windows for one description (preserving section headers)."""
    spans: list[list[int]] = []
    for m in _EDU_NET.finditer(text):
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
    per_ats = _arg("--per-ats", 60)
    per_company = _arg("--per-company", 15)
    max_per_company = _arg("--max-per-company", 3)
    max_total = _arg("--max-total", 700)
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "data" / "degree_candidates.jsonl"

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
                    {
                        "id": job.id,
                        "source": ats,
                        "company_key": key,
                        "title": job.title,
                        "text": win,
                    }
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

    # Dedup identical windows; cap total.
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
