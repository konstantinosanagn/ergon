"""Provider `normalize()` mapping of structured ATS fields into JobPosting.

Covers fields where the enrichment layer's ``if job.level is JobLevel.UNKNOWN`` guard
means a provider-set value is authoritative and must be mapped correctly at the source.
"""

from ergon_tracker.models import JobLevel
from ergon_tracker.providers.base import RawJob
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider


def _raw(payload):
    return RawJob(
        source="smartrecruiters",
        source_job_id="1",
        company="Co",
        url="http://x",
        token="co",
        payload=payload,
    )


def test_smartrecruiters_maps_experience_level():
    p = SmartRecruitersProvider()
    job = p.normalize(
        _raw(
            {
                "name": "Engineer",
                "experienceLevel": {"id": "mid_senior_level", "label": "Mid-Senior Level"},
            }
        )
    )
    assert job.level is JobLevel.SENIOR


def test_smartrecruiters_unknown_level_stays_unknown():
    p = SmartRecruitersProvider()
    job = p.normalize(_raw({"name": "Engineer"}))
    assert job.level is JobLevel.UNKNOWN


def test_jazzhr_maps_experience():
    from ergon_tracker.providers.jazzhr import JazzHRProvider

    p = JazzHRProvider()
    job = p.normalize(_raw({"title": "Engineer", "experience": "Experienced"}))
    assert job.level is JobLevel.MID


def test_workable_maps_experience():
    from ergon_tracker.providers.workable import WorkableProvider

    p = WorkableProvider()
    job = p.normalize(_raw({"title": "Engineer", "experience": "Entry level"}))
    assert job.level is JobLevel.ENTRY


def test_workable_unknown_experience_stays_unknown():
    from ergon_tracker.providers.workable import WorkableProvider

    p = WorkableProvider()
    job = p.normalize(_raw({"title": "Engineer"}))
    assert job.level is JobLevel.UNKNOWN


def test_join_maps_structured_salary():
    from ergon_tracker.models import SalaryInterval
    from ergon_tracker.providers.join import JoinProvider

    p = JoinProvider()
    job = p.normalize(
        _raw(
            {
                "title": "Eng",
                "salaryAmountFrom": {"amount": 18000000, "currency": "USD"},
                "salaryAmountTo": {"amount": 32000000, "currency": "USD"},
                "salaryFrequency": "PER_YEAR",
                "settings": {"showSalary": False},
            }
        )
    )
    assert job.salary is not None
    assert job.salary.min_amount == 180000.0 and job.salary.max_amount == 320000.0
    assert job.salary.currency == "USD" and job.salary.interval is SalaryInterval.YEAR


def test_join_no_amount_stays_none():
    from ergon_tracker.providers.join import JoinProvider

    p = JoinProvider()
    assert p.normalize(_raw({"title": "Eng"})).salary is None


def test_personio_promotes_seniority_and_years():
    from ergon_tracker.providers.personio import PersonioProvider

    p = PersonioProvider()
    job = p.normalize(_raw({"name": "Eng", "seniority": "senior", "yearsOfExperience": "1-2"}))
    assert job.level is JobLevel.SENIOR
    assert job.years_experience_min == 1 and job.years_experience_max == 2


def test_personio_unknown_seniority_stays_unknown():
    from ergon_tracker.providers.personio import PersonioProvider

    p = PersonioProvider()
    job = p.normalize(_raw({"name": "Eng"}))
    assert job.level is JobLevel.UNKNOWN
    assert job.years_experience_min is None and job.years_experience_max is None


def test_personio_years_range_parsing():
    from ergon_tracker.providers.personio import _years_range

    assert _years_range("lt-1") == (0, 1)
    assert _years_range("1-2") == (1, 2)
    assert _years_range("5-10") == (5, 10)
    assert _years_range("gt-10") == (10, None)
    assert _years_range("") == (None, None)
    assert _years_range(None) == (None, None)


def test_breezy_parses_freetext_salary():
    from ergon_tracker.providers.breezy import BreezyProvider

    p = BreezyProvider()
    job = p.normalize(_raw({"name": "Eng", "salary": "$78,000 / year"}))
    assert job.salary is not None and job.salary.min_amount == 78000.0


def test_breezy_empty_salary_stays_none():
    from ergon_tracker.providers.breezy import BreezyProvider

    p = BreezyProvider()
    assert p.normalize(_raw({"name": "Eng", "salary": ""})).salary is None


def test_coveo_direct_mode_reads_correct_keys():
    """Direct-mode (UST-style) raw items key description under 'data' and department under
    'obu' — not the proxy-mode 'description'/'category' keys. normalize() must read both."""
    from ergon_tracker.providers.coveo import CoveoProvider

    p = CoveoProvider()
    job = p.normalize(_raw({"title": "Eng", "data": "<p>Build things.</p>", "obu": "Engineering"}))
    assert job.description_html is not None and "Build things" in job.description_html
    assert job.department == "Engineering"


def test_coveo_proxy_mode_still_reads_original_keys():
    """Proxy-mode (SLB-style) raw items key description/department under 'description'/'category'.
    Fixing direct-mode must not regress proxy-mode, and the proxy key must take precedence."""
    from ergon_tracker.providers.coveo import CoveoProvider

    p = CoveoProvider()
    job = p.normalize(
        _raw({"title": "Eng", "description": "<p>Ship code.</p>", "category": "Product"})
    )
    assert job.description_html is not None and "Ship code" in job.description_html
    assert job.department == "Product"
