"""Benchmark-driven tri-state accuracy gate for the visa-sponsorship detector.

Real JD windows that mention "sponsor" (``scripts/build_sponsorship_corpus.py``), blind-labeled for
the posting's stated policy: True (offered) / False (won't sponsor) / null (no policy or an unrelated
"sponsor" sense — event/project/executive sponsor). ``detect_sponsorship(text)`` returns the same
tri-state. Measured tri-state accuracy (2026-07-06: 98.9%).

Record format: ``{"text": ..., "sponsorship": true|false|null, "src": ...}``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.sponsorship import detect_sponsorship

CORPUS_PATH = Path(__file__).parent / "fixtures" / "sponsorship_corpus.jsonl"

ACCURACY_GATE = 0.95  # margin below the measured 98.9%


def _load() -> list[dict]:
    return [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]


def _gold(v) -> bool | None:
    return v if v in (True, False) else None


def test_corpus_is_substantial() -> None:
    records = _load()
    assert len(records) >= 120, f"only {len(records)} records"
    stated = [r for r in records if _gold(r["sponsorship"]) is not None]
    assert len(stated) >= 60, f"only {len(stated)} stated (true/false) records"


def test_tristate_accuracy() -> None:
    records = _load()
    hits, misses = 0, []
    for r in records:
        got = detect_sponsorship(r["text"])
        if got == _gold(r["sponsorship"]):
            hits += 1
        else:
            misses.append((r["sponsorship"], got, r["text"]))
    acc = hits / len(records)
    print(f"\nsponsorship tri-state accuracy: {hits}/{len(records)} = {acc:.1%}")
    if acc < ACCURACY_GATE:
        detail = "\n".join(f"  want={w} got={g} :: {t[:100]!r}" for w, g, t in misses[:10])
        pytest.fail(f"accuracy {acc:.1%} below gate {ACCURACY_GATE:.0%}.\n{detail}")
