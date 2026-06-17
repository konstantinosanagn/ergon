"""Tests for enrichment (level/geo/sector) and the advanced SearchQuery filters."""

from __future__ import annotations

import pytest

from ergon_tracker import (
    JobLevel,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SearchQuery,
)
from ergon_tracker.enrich import enrich_in_place, infer_level, load_sector_index, normalize_geo


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineer Intern", JobLevel.INTERN),
        ("VP of Engineering", JobLevel.EXECUTIVE),
        ("Chief Technology Officer", JobLevel.EXECUTIVE),
        ("Director of Product", JobLevel.DIRECTOR),
        ("Senior Engineering Manager", JobLevel.MANAGER),
        ("Principal Scientist", JobLevel.PRINCIPAL),
        ("Staff Software Engineer", JobLevel.STAFF),
        ("Tech Lead, Payments", JobLevel.LEAD),
        ("Sr. Backend Engineer", JobLevel.SENIOR),
        ("Junior Data Analyst", JobLevel.JUNIOR),
        ("New Grad Software Engineer", JobLevel.ENTRY),
        ("Software Engineer", JobLevel.UNKNOWN),
    ],
)
def test_infer_level(title: str, expected: JobLevel) -> None:
    assert infer_level(title) == expected


def test_normalize_geo_us_city_state() -> None:
    loc = Location(raw="Santa Clara, CA")
    normalize_geo(loc)
    assert loc.city == "Santa Clara"
    assert loc.region == "CA"
    assert loc.country == "United States"


def test_normalize_geo_city_country_and_remote() -> None:
    loc = Location(raw="Berlin, Germany")
    normalize_geo(loc)
    assert loc.city == "Berlin"
    assert loc.country == "Germany"

    rem = Location(raw="Remote - United States")
    normalize_geo(rem)
    assert rem.is_remote is True
    assert rem.country == "United States"


def test_normalize_geo_deterministic_country_cases() -> None:
    # country from city gazetteer (no explicit country in the string)
    sf = Location(raw="San Francisco")
    normalize_geo(sf)
    assert sf.country == "United States"
    assert sf.city == "San Francisco"

    lon = Location(raw="London")
    normalize_geo(lon)
    assert lon.country == "United Kingdom"

    # ATS "Locations" suffix is stripped before matching the country
    de = Location(raw="Germany Locations")
    normalize_geo(de)
    assert de.country == "Germany"

    # "US-Remote" hyphen token resolves to the US and flags remote
    usr = Location(raw="US-Remote")
    normalize_geo(usr)
    assert usr.country == "United States"
    assert usr.is_remote is True

    # metro/bay-area noise -> city, then gazetteer -> country
    bay = Location(raw="San Francisco Bay Area")
    normalize_geo(bay)
    assert bay.country == "United States"

    # "3 Locations" has no place -> no bogus city
    nl = Location(raw="3 Locations")
    normalize_geo(nl)
    assert nl.city is None


def test_sector_index_loads_and_resolves() -> None:
    idx = load_sector_index()
    assert len(idx) >= 200
    assert idx.get(key="stripe")
    assert idx.get(domain="figma.com")
    assert idx.get(key="does-not-exist") is None


def test_enrich_in_place_sets_level_and_geo() -> None:
    job = JobPosting.create(
        source="s",
        source_job_id="1",
        company="Acme",
        title="Staff Software Engineer",
        locations=[Location(raw="Austin, TX")],
    )
    enrich_in_place(job)
    assert job.level is JobLevel.STAFF
    assert job.locations[0].country == "United States"


# --- advanced SearchQuery filters ------------------------------------------


def _job(**kw: object) -> JobPosting:
    base = {"source": "s", "source_job_id": "1", "company": "Acme", "title": "Engineer", **kw}
    return JobPosting.create(**base)  # type: ignore[arg-type]


def test_salary_filter_overlap_and_unknown() -> None:
    paid = _job(salary=Salary(min_amount=120_000, max_amount=160_000, currency="USD"))
    assert SearchQuery(salary_min=100_000).matches(paid)
    assert SearchQuery(salary_max=130_000).matches(paid)
    assert not SearchQuery(salary_min=200_000).matches(paid)
    assert not SearchQuery(salary_max=100_000).matches(paid)
    # currency mismatch is excluded when a currency is specified
    assert not SearchQuery(salary_min=100_000, salary_currency="EUR").matches(paid)

    nopay = _job()
    assert SearchQuery(salary_min=100_000).matches(nopay)  # default: keep unknown
    assert not SearchQuery(salary_min=100_000, include_unknown_salary=False).matches(nopay)


def test_level_filter() -> None:
    job = _job(title="Staff Engineer", level=JobLevel.STAFF)
    assert SearchQuery(level=JobLevel.STAFF).matches(job)
    assert not SearchQuery(level=JobLevel.SENIOR).matches(job)


def test_sector_filter() -> None:
    job = _job(sector="Fintech")
    assert SearchQuery(sector="fintech").matches(job)
    assert not SearchQuery(sector="Gaming").matches(job)


def test_geo_filter_country_and_city() -> None:
    job = _job(locations=[Location(city="Berlin", country="Germany", raw="Berlin, Germany")])
    assert SearchQuery(country="Germany").matches(job)
    assert SearchQuery(city="berlin").matches(job)
    assert not SearchQuery(country="France").matches(job)


def test_remote_unaffected_by_new_filters() -> None:
    job = _job(remote=RemoteType.REMOTE, locations=[Location(is_remote=True, raw="Remote")])
    assert SearchQuery(remote=True).matches(job)
