"""Live end-to-end verification that EVERY search filter works on real enriched jobs.

Pulls a no-filter pool across mixed ATS, reports per-field enrichment coverage, then applies
each filter and asserts (a) every passing job actually satisfies it and (b) the filter is
active (excludes some). Run: .venv/bin/python scripts/verify_filters.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ergon_tracker  # noqa: E402
from ergon_tracker.models import EmploymentType, JobLevel, RemoteType, SearchQuery  # noqa: E402

COMPANIES = [
    "ramp.com", "stripe.com", "spotify.com", "notion.so", "adobe.com",
    "visa.com", "figma.com", "datadog.com",
]


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({(n / d * 100) if d else 0:.0f}%)"


def main() -> None:
    pool = ergon_tracker.search(companies=COMPANIES, limit=400).jobs
    n = len(pool)
    print(f"enriched pool: {n} jobs across {len(COMPANIES)} companies\n")

    print("=== per-field enrichment coverage ===")
    print(f"  level known : {pct(sum(j.level is not JobLevel.UNKNOWN for j in pool), n)}")
    print(f"  remote known: {pct(sum(j.remote is not RemoteType.UNKNOWN for j in pool), n)}")
    print(f"  emp_type    : {pct(sum(j.employment_type is not EmploymentType.UNKNOWN for j in pool), n)}")
    print(f"  salary      : {pct(sum(j.salary is not None for j in pool), n)}")
    print(f"  years_exp   : {pct(sum(j.years_experience_min is not None for j in pool), n)}")
    print(f"  country     : {pct(sum(bool(j.locations and j.locations[0].country) for j in pool), n)}")
    print(f"  city        : {pct(sum(bool(j.locations and j.locations[0].city) for j in pool), n)}")
    print(f"  sector      : {pct(sum(j.sector is not None for j in pool), n)}")
    print(f"  posted_at   : {pct(sum(j.posted_at is not None for j in pool), n)}")

    # (label, query, predicate the passing jobs MUST satisfy)
    checks: list[tuple[str, SearchQuery, object]] = [
        ("keywords='engineer'", SearchQuery(keywords="engineer"),
         lambda j: "engineer" in (j.title or "").lower() or "engineer" in (j.description_text or "").lower()),
        ("remote=True", SearchQuery(remote=True),
         lambda j: j.remote in (RemoteType.REMOTE, RemoteType.HYBRID) or any(loc.is_remote for loc in j.locations) or j.remote is RemoteType.UNKNOWN),
        ("level=SENIOR", SearchQuery(level=JobLevel.SENIOR), lambda j: j.level is JobLevel.SENIOR),
        ("employment_type=FULL_TIME", SearchQuery(employment_type=EmploymentType.FULL_TIME),
         lambda j: j.employment_type in (EmploymentType.FULL_TIME, EmploymentType.UNKNOWN)),
        ("sector='Fintech'", SearchQuery(sector="Fintech"),
         lambda j: "fintech" in (j.sector or "").lower()),
        ("country='United States'", SearchQuery(country="United States"),
         lambda j: any((loc.country or "").lower() == "united states" or "united states" in (loc.raw or "").lower() for loc in j.locations)),
        ("city='New York'", SearchQuery(city="New York"),
         lambda j: any((loc.city or "").lower() == "new york" or "new york" in (loc.raw or "").lower() for loc in j.locations)),
        ("salary_min=200000 (excl unknown)", SearchQuery(salary_min=200000, include_unknown_salary=False),
         lambda j: j.salary is not None and (j.salary.max_amount or j.salary.min_amount or 0) >= 200000),
        ("salary_max=120000 (excl unknown)", SearchQuery(salary_max=120000, include_unknown_salary=False),
         lambda j: j.salary is not None and (j.salary.min_amount or j.salary.max_amount or 1e9) <= 120000),
        ("min_years=5 (excl unknown)", SearchQuery(min_years=5, include_unknown_years=False),
         lambda j: (j.years_experience_max or j.years_experience_min or 0) >= 5),
        ("max_years=2 (excl unknown)", SearchQuery(max_years=2, include_unknown_years=False),
         lambda j: (j.years_experience_min or j.years_experience_max or 99) <= 2),
    ]

    print("\n=== filter correctness (passers must satisfy the filter) ===")
    for label, q, pred in checks:
        passed = [j for j in pool if q.matches(j)]
        violations = [j for j in passed if not pred(j)]  # type: ignore[operator]
        status = "OK" if not violations else f"FAIL ({len(violations)} violations)"
        active = "active" if len(passed) < n else "passes-all"
        print(f"  {label:36s} -> {len(passed):>3d}/{n} kept  [{status}, {active}]")


if __name__ == "__main__":
    main()
