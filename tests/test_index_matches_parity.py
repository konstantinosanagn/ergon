"""Property test: the index SQL filter and SearchQuery.matches() must agree on EVERY structured
filter. Both operate on the SAME stored set (jobs read back from the index), so this isolates the
WHERE-clause vs matches() logic. Catches any current or future drift between the two paths.

Keywords are excluded on purpose: the index ranks via FTS (porter stemming) while matches() does
substring containment — a deliberate, separately-tested difference. This fuzzes the structured
filters, where every parity bug has lived.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.models import (
    EmploymentType,
    JobLevel,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SearchQuery,
)

_SECTORS = ["Fintech", "AI/ML", "Healthcare", None]
_CITIES = ["New York", "New York City", "Brooklyn", "San Francisco", "Austin", None]
_COUNTRIES = ["United States", "Canada", "Germany", None]
_REMOTE = list(RemoteType)
_LEVELS = list(JobLevel)
_EMP = list(EmploymentType)
_CCY = ["USD", "EUR", "GBP", None]
# tz-aware UTC datetimes so the index's ISO-string compare and matches()' datetime compare agree
_POSTED = [
    None,
    datetime(2026, 1, 1, tzinfo=timezone.utc),
    datetime(2026, 4, 1, tzinfo=timezone.utc),
    datetime(2026, 6, 1, tzinfo=timezone.utc),
]
_CUTOFFS = [datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 5, 15, tzinfo=timezone.utc)]


def _make_jobs(rng, n):
    jobs = []
    for i in range(n):
        has_sal = rng.random() < 0.6
        lo = rng.choice([None, 80000, 120000, 150000]) if has_sal else None
        hi = rng.choice([None, 130000, 180000, 220000]) if has_sal else None
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        sal = (
            Salary(min_amount=lo, max_amount=hi, currency=rng.choice(_CCY)) if (lo or hi) else None
        )
        ymin = rng.choice([None, 0, 2, 5, 8])
        ymax = rng.choice([None, 1, 3, 7, 10])
        if ymin is not None and ymax is not None and ymin > ymax:
            ymin, ymax = ymax, ymin
        city = rng.choice(_CITIES)
        country = rng.choice(_COUNTRIES)
        raw = ", ".join(x for x in (city, country) if x) or "Remote"
        jobs.append(
            JobPosting.create(
                source="greenhouse",
                source_job_id=str(i),
                company=f"Co{i % 7}",
                title=f"Engineer Role {i}",  # distinct -> survives fuzzy dedup
                sector=rng.choice(_SECTORS),
                level=rng.choice(_LEVELS),
                remote=rng.choice(_REMOTE),
                employment_type=rng.choice(_EMP),
                salary=sal,
                years_experience_min=ymin,
                years_experience_max=ymax,
                visa_sponsor=rng.choice([True, None]),
                sponsorship_offered=rng.choice([True, False, None]),
                posted_at=rng.choice(_POSTED),
                locations=[Location(raw=raw, city=city, country=country)],
            )
        )
    return jobs


def _random_query(rng):
    q = {"limit": 100000}
    if rng.random() < 0.5:
        q["level"] = rng.choice(_LEVELS)
        q["include_unknown_level"] = rng.random() < 0.5
    if rng.random() < 0.4:
        q["sector"] = rng.choice([s for s in _SECTORS if s])
        q["include_unknown_sector"] = rng.random() < 0.5
    if rng.random() < 0.4:
        q["city"] = rng.choice([c for c in _CITIES if c])
    if rng.random() < 0.3:
        q["country"] = rng.choice(["USA", "US", "United States", "Canada", "Germany"])
    if rng.random() < 0.3:
        q["remote"] = True
    if rng.random() < 0.4:
        q["salary_min"] = rng.choice([100000, 140000, 160000])
        q["include_unknown_salary"] = rng.random() < 0.5
    if rng.random() < 0.3:
        q["salary_max"] = rng.choice([150000, 200000])
        q["include_unknown_salary"] = rng.random() < 0.5
    if rng.random() < 0.3:
        q["salary_currency"] = rng.choice(["USD", "EUR"])
        q.setdefault("salary_min", 100000)
    if rng.random() < 0.3:
        q["min_years"] = rng.choice([2, 5])
        q["include_unknown_years"] = rng.random() < 0.5
    if rng.random() < 0.3:
        q["max_years"] = rng.choice([3, 7])
        q["include_unknown_years"] = rng.random() < 0.5
    if rng.random() < 0.25:
        q["employment_type"] = rng.choice(_EMP)
    if rng.random() < 0.3:
        q["posted_after"] = rng.choice(_CUTOFFS)
    if rng.random() < 0.2:
        q["visa_sponsor"] = True
    if rng.random() < 0.2:
        q["sponsorship_offered"] = rng.choice([True, False])
        q["include_unknown_sponsorship"] = rng.random() < 0.5
    return SearchQuery(**q)


def test_index_filter_parity_with_matches(tmp_path):
    rng = random.Random(1234)  # deterministic
    p = tmp_path / "i.sqlite"
    build_index(_make_jobs(rng, 80), p, build_id="b1")
    backend = SqliteIndexBackend(p)
    stored = backend.search(SearchQuery(limit=100000))  # the canonical reconstructed set
    assert stored, "index should contain jobs"

    mismatches = []
    for _ in range(200):
        q = _random_query(rng)
        index_ids = {j.id for j in backend.search(q)}
        oracle_ids = {j.id for j in stored if q.matches(j)}
        if index_ids != oracle_ids:
            mismatches.append((q.model_dump(exclude_none=True), index_ids ^ oracle_ids))
    assert not mismatches, f"{len(mismatches)} index/matches() divergences, e.g. {mismatches[0]}"
