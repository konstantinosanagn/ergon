"""Provider `normalize()` mapping of structured ATS fields into JobPosting.

Covers fields where the enrichment layer's ``if job.level is JobLevel.UNKNOWN`` guard
means a provider-set value is authoritative and must be mapped correctly at the source.
"""

from ergon_tracker.models import JobLevel
from ergon_tracker.providers.base import RawJob
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider


def _raw(payload):
    return RawJob(source="smartrecruiters", source_job_id="1", company="Co", url="http://x", token="co", payload=payload)


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
