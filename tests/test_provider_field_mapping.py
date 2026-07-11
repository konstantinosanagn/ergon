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
