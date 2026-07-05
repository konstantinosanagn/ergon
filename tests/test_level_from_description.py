"""High-precision description-based level cues (entry/intern only)."""

from __future__ import annotations

import pytest

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.extract.level import level_from_description
from ergon_tracker.models import JobLevel, JobPosting


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We welcome new grads to apply.", JobLevel.ENTRY),
        ("This is an entry-level role on our platform team.", JobLevel.ENTRY),
        ("Great early-career opportunity.", JobLevel.ENTRY),
        ("Recent graduates encouraged to apply.", JobLevel.ENTRY),
        ("No prior experience required — we train you.", JobLevel.ENTRY),
        ("Summer internship in our NYC office.", JobLevel.INTERN),
        ("Join our internship program.", JobLevel.INTERN),
        # high-precision: must NOT fire on noisy prose
        ("You hold a graduate degree in CS.", JobLevel.UNKNOWN),
        ("Report to senior leadership and graduate-level researchers.", JobLevel.UNKNOWN),
        ("5+ years building distributed systems.", JobLevel.UNKNOWN),
        ("", JobLevel.UNKNOWN),
        (None, JobLevel.UNKNOWN),
    ],
)
def test_level_from_description(text, expected):
    assert level_from_description(text) == expected


def test_enrich_uses_description_level_when_title_bare():
    # bare title, JD says new grad -> entry (only when infer flag on)
    j = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        description_text="We are hiring new grads for our backend team. No prior experience required.",
    )
    enrich_in_place(j, company_key="acme", infer_level_from_experience=True)
    assert j.level is JobLevel.ENTRY


def test_enrich_explicit_off_keeps_title_only():
    # flag explicitly off: description cues are NOT applied -> bare title stays unknown
    j = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        description_text="We welcome new grads!",
    )
    enrich_in_place(j, company_key="acme", infer_level_from_experience=False)
    assert j.level is JobLevel.UNKNOWN


def test_enrich_default_on_applies_description_cues():
    # NEW default (flag on): description cues fill in when the title gives no level
    j = JobPosting.create(
        source="greenhouse",
        source_job_id="2",
        company="Acme",
        title="Software Engineer",
        description_text="We welcome new grads!",
    )
    enrich_in_place(j, company_key="acme")
    assert j.level is JobLevel.ENTRY


def test_title_level_still_wins_over_description():
    j = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Senior Software Engineer",
        description_text="Open to new grads too.",
    )
    enrich_in_place(j, company_key="acme", infer_level_from_experience=True)
    assert j.level is JobLevel.SENIOR  # title beats description
