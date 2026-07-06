"""Benchmark-driven per-field accuracy gate for the geo (country/city) normalizer.

Real DISTINCT location strings fetched from live postings (``scripts/build_geo_corpus.py``),
blind-labeled for the country and city they state (per the labeling guide). ``geo.py``'s
``normalize_geo(Location(raw=...))`` fills ``.city``/``.country``; this measures each field's accuracy
against the human labels, null-aware and case-insensitive.

Record format: ``{"location_raw": "Austin, TX", "country": "United States", "city": "Austin",
"src": ...}`` — ``country``/``city`` are ``null`` when the string states none.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.geo import normalize_geo
from ergon_tracker.models import Location

CORPUS_PATH = Path(__file__).parent / "fixtures" / "geo_corpus.jsonl"

# Ratcheting gates — a margin below the measured numbers (2026-07-06, 800 distinct enterprise
# location strings: country 94.8%, city 88.9%); raise as geo.py improves. This corpus is deliberately
# enterprise-HRIS-heavy (taleo/dejobs/peoplesoft), which is where the ISO-code and dash-format parsing
# lived; the remaining tail is bare US cities not in the gazetteer + foreign hyphenated region names.
COUNTRY_ACC_GATE = 0.90
CITY_ACC_GATE = 0.84


def _load() -> list[dict]:
    return [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]


def _norm(v: str | None) -> str | None:
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _predict(raw: str) -> tuple[str | None, str | None]:
    loc = normalize_geo(Location(raw=raw))
    return _norm(loc.country), _norm(loc.city)


def test_corpus_is_substantial() -> None:
    records = _load()
    assert len(records) >= 300, f"only {len(records)} records in corpus"
    assert sum(1 for r in records if r["country"]) >= 150, "too few country-bearing records"


def test_country_accuracy() -> None:
    records = _load()
    hits, misses = 0, []
    for r in records:
        got_c, _ = _predict(r["location_raw"])
        if got_c == _norm(r["country"]):
            hits += 1
        else:
            misses.append((r["location_raw"], r["country"], got_c))
    acc = hits / len(records)
    print(f"\ngeo country accuracy: {hits}/{len(records)} = {acc:.1%}")
    if acc < COUNTRY_ACC_GATE:
        detail = "\n".join(f"  {raw!r} want={w} got={g}" for raw, w, g in misses[:10])
        pytest.fail(f"country accuracy {acc:.1%} below gate {COUNTRY_ACC_GATE:.0%}.\n{detail}")


def test_city_accuracy() -> None:
    records = _load()
    hits, misses = 0, []
    for r in records:
        _, got_city = _predict(r["location_raw"])
        if got_city == _norm(r["city"]):
            hits += 1
        else:
            misses.append((r["location_raw"], r["city"], got_city))
    acc = hits / len(records)
    print(f"geo city accuracy: {hits}/{len(records)} = {acc:.1%}")
    if acc < CITY_ACC_GATE:
        detail = "\n".join(f"  {raw!r} want={w} got={g}" for raw, w, g in misses[:10])
        pytest.fail(f"city accuracy {acc:.1%} below gate {CITY_ACC_GATE:.0%}.\n{detail}")
