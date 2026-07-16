"""Tests for scripts.bench.parity: client (SearchQuery.matches()) vs index-SQL (search_rows())
agreement on structured filters, plus the two documented by-design divergences.

All inputs are synthetic, in-memory ``JobPosting``/``SearchQuery`` objects -- no real index build,
no fixture files, no network. ``sql_accepts``/``agree`` DO run genuine sqlite (a one-row scratch
table, see ``parity._scratch_index``), but that's an implementation detail of the parity helper
under test, not something this test file sets up itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.bench.parity import (
    agree,
    check_row,
    divergence_rate_keywords,
    flag_sql_only_filters,
    sql_accepts,
)

from ergon_tracker.models import (
    EmploymentType,
    JobLevel,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SearchQuery,
)


def _job(**overrides: object) -> JobPosting:
    defaults: dict[str, object] = {
        "source": "greenhouse",
        "source_job_id": "1",
        "company": "Acme Corp",
        "title": "Senior Backend Engineer",
        "description_text": "We build payments infrastructure at scale.",
        "locations": [Location(city="New York", country="United States")],
        "remote": RemoteType.ONSITE,
        "level": JobLevel.SENIOR,
        "employment_type": EmploymentType.FULL_TIME,
        "sector": "Fintech",
        "salary": Salary(min_amount=150000, max_amount=180000, currency="USD"),
        "years_experience_min": 5,
        "years_experience_max": 8,
        "degree_min": "bachelor",
        "degree_required": False,
        "sponsorship_offered": True,
        "visa_sponsor": True,
        "posted_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    defaults.update(overrides)
    return JobPosting.create(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# check_row() -- the client-side path
# ---------------------------------------------------------------------------


def test_check_row_true_when_job_matches_structured_filter():
    job = _job()
    query = SearchQuery(level=JobLevel.SENIOR)
    assert check_row(query, job) is True


def test_check_row_false_when_job_fails_structured_filter():
    job = _job()
    query = SearchQuery(level=JobLevel.JUNIOR)
    assert check_row(query, job) is False


# ---------------------------------------------------------------------------
# agree() -- a job that matches client-side must be reported as agreeing across every
# structured filter (level, country, city, remote, sector, employment_type, salary, years,
# degree, sponsorship, visa, recency).
# ---------------------------------------------------------------------------


def test_agree_on_matching_level_filter():
    job = _job()
    query = SearchQuery(level=JobLevel.SENIOR)
    assert check_row(query, job) is True
    assert agree(query, job) is True


def test_agree_on_non_matching_level_filter():
    job = _job()
    query = SearchQuery(level=JobLevel.JUNIOR)
    assert check_row(query, job) is False
    assert agree(query, job) is True


def test_agree_on_country_filter():
    job = _job()
    assert agree(SearchQuery(country="United States"), job) is True
    assert agree(SearchQuery(country="Germany"), job) is True


def test_agree_on_city_filter():
    job = _job()
    assert agree(SearchQuery(city="New York"), job) is True
    assert agree(SearchQuery(city="London"), job) is True


def test_agree_on_remote_filter():
    onsite = _job()
    remote_job = _job(source_job_id="2", remote=RemoteType.REMOTE)
    assert agree(SearchQuery(remote=True), onsite) is True
    assert agree(SearchQuery(remote=True), remote_job) is True


def test_agree_on_sector_filter():
    job = _job()
    assert agree(SearchQuery(sector="Fintech"), job) is True
    assert agree(SearchQuery(sector="Healthcare"), job) is True


def test_agree_on_employment_type_filter():
    job = _job()
    assert agree(SearchQuery(employment_type=EmploymentType.FULL_TIME), job) is True
    assert agree(SearchQuery(employment_type=EmploymentType.CONTRACT), job) is True


def test_agree_on_salary_filter():
    job = _job()
    assert agree(SearchQuery(salary_min=100000, salary_max=200000), job) is True
    assert agree(SearchQuery(salary_min=300000), job) is True


def test_agree_on_years_filter():
    job = _job()
    assert agree(SearchQuery(min_years=3, max_years=10), job) is True
    assert agree(SearchQuery(min_years=20), job) is True


def test_agree_on_degree_filter():
    job = _job()
    assert agree(SearchQuery(max_degree="master"), job) is True
    assert agree(SearchQuery(max_degree="highschool"), job) is True


def test_agree_on_sponsorship_filter():
    job = _job()
    assert agree(SearchQuery(sponsorship_offered=True), job) is True
    assert (
        agree(SearchQuery(sponsorship_offered=False, include_unknown_sponsorship=False), job)
        is True
    )


def test_agree_on_visa_sponsor_filter():
    job = _job()
    assert agree(SearchQuery(visa_sponsor=True), job) is True
    no_sponsor = _job(source_job_id="3", visa_sponsor=None)
    assert agree(SearchQuery(visa_sponsor=True), no_sponsor) is True


def test_agree_on_recency_filter_max_age_days():
    fresh = _job()  # posted 1 day ago
    stale = _job(source_job_id="4", posted_at=datetime.now(timezone.utc) - timedelta(days=40))
    query = SearchQuery(max_age_days=7)
    assert check_row(query, fresh) is True
    assert check_row(query, stale) is False
    assert agree(query, fresh) is True
    assert agree(query, stale) is True


def test_agree_across_combined_structured_filters():
    job = _job()
    query = SearchQuery(
        level=JobLevel.SENIOR,
        country="United States",
        city="New York",
        sector="Fintech",
        employment_type=EmploymentType.FULL_TIME,
        salary_min=100000,
        min_years=3,
        max_degree="master",
        sponsorship_offered=True,
        visa_sponsor=True,
        max_age_days=30,
    )
    assert check_row(query, job) is True
    assert agree(query, job) is True


# ---------------------------------------------------------------------------
# Known, by-design divergences
# ---------------------------------------------------------------------------


def test_flag_sql_only_filters_empty_when_unset():
    assert flag_sql_only_filters(SearchQuery()) == []


def test_flag_sql_only_filters_reports_max_last_seen_age_days():
    query = SearchQuery(max_last_seen_age_days=14)
    assert flag_sql_only_filters(query) == ["max_last_seen_age_days"]
    # matches() has no concept of it at all -- setting it never changes the client verdict.
    job = _job()
    plain = SearchQuery()
    assert check_row(plain, job) == check_row(query, job)


def test_divergence_rate_keywords_zero_with_no_keyword_pairs():
    job = _job()
    pairs = [(SearchQuery(level=JobLevel.SENIOR), job)]
    assert divergence_rate_keywords(pairs) == 0.0


def test_divergence_rate_keywords_detects_snippet_truncation_gap():
    # A keyword that only appears well past the FTS snippet's 300-char cutoff: matches() (full
    # description_text) accepts client-side, but the index's FTS content (title/company/
    # department/snippet only) can't see it -- the documented structural divergence source.
    long_desc = ("Great team, great mission. " * 20) + "Requires prior kubernetes experience."
    assert len(long_desc) > 300
    job = _job(description_text=long_desc)
    query = SearchQuery(keywords="kubernetes")
    assert check_row(query, job) is True
    assert sql_accepts(query, job) is False
    rate = divergence_rate_keywords([(query, job)])
    assert rate == 1.0


def test_divergence_rate_keywords_agrees_when_keyword_in_title():
    job = _job()  # title contains "Backend Engineer"
    query = SearchQuery(keywords="backend engineer")
    assert check_row(query, job) is True
    assert sql_accepts(query, job) is True
    assert divergence_rate_keywords([(query, job)]) == 0.0


def test_agree_ignores_keyword_divergence():
    # agree() strips keywords before comparing, so a query that WOULD diverge on keywords still
    # reports structured-filter agreement.
    long_desc = ("Great team, great mission. " * 20) + "Requires prior kubernetes experience."
    job = _job(description_text=long_desc)
    query = SearchQuery(keywords="kubernetes", level=JobLevel.SENIOR)
    assert agree(query, job) is True
