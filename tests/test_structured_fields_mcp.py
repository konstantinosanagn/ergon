"""End-to-end real-serving-path stress test (Task 12 of the structured-field-recovery plan).

The whole Stage-1 design rests on ONE invariant: ``enrich_in_place`` (``ergon_tracker.enrich``)
must PRESERVE provider-set structured fields (level/salary/degree/years) rather than overwrite
them with a text-extractor guess. Its guards are:

    if job.level is JobLevel.UNKNOWN: ...
    if job.salary is None: ...
    if job.years_experience_min is None and job.years_experience_max is None: ...
    if job.degree_min is None and job.degree_required is None: ...
    if job.sector is None: ...

Each test below builds a synthetic-but-realistic ``RawJob`` payload (real field shape for that
ATS), runs it through the REAL ``provider.normalize()`` and then the REAL ``enrich_in_place()``,
and asserts the provider-set value survived enrichment unchanged. This is offline-only: no
network, no live ATS calls — synthetic payloads through the real serving code.

The final test drives the real query/serving path (``SearchQuery.matches``) to prove a recovered
field actually changes filter results, not just that it survives in isolation.
"""

from __future__ import annotations

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.models import JobLevel, SearchQuery
from ergon_tracker.providers.base import RawJob
from ergon_tracker.providers.breezy import BreezyProvider
from ergon_tracker.providers.jazzhr import JazzHRProvider
from ergon_tracker.providers.join import JoinProvider
from ergon_tracker.providers.personio import PersonioProvider
from ergon_tracker.providers.recruitee import RecruiteeProvider
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider
from ergon_tracker.providers.workable import WorkableProvider


def _raw(source: str, payload: dict, *, source_job_id: str = "1", company: str = "Co") -> RawJob:
    return RawJob(
        source=source,
        source_job_id=source_job_id,
        company=company,
        url="http://x.example/job/1",
        token="co",
        payload=payload,
    )


# --- smartrecruiters: level (experienceLevel) --------------------------------


def test_smartrecruiters_level_survives_enrichment():
    # No description at all, so the text extractor sees nothing and would return UNKNOWN if it
    # ever ran — the point is that the provider-set level short-circuits it via the enrich guard.
    prov = SmartRecruitersProvider()
    job = prov.normalize(
        _raw(
            "smartrecruiters",
            {"name": "Platform Coordinator", "experienceLevel": {"label": "Senior Level"}},
        )
    )
    assert job.level is JobLevel.SENIOR  # mapped by normalize()

    enrich_in_place(job)

    assert job.level is JobLevel.SENIOR  # provider value preserved end-to-end


# --- jazzhr: level (experience) ------------------------------------------------


def test_jazzhr_level_survives_enrichment():
    prov = JazzHRProvider()
    job = prov.normalize(
        _raw(
            "jazzhr",
            {
                "id": "job_20260605135030_abc123",
                "title": "Widget Assembler",
                "experience": "Experienced",
                "description": "<p>Standard duties, nothing seniority-specific here.</p>",
            },
        )
    )
    assert job.level is JobLevel.MID  # "Experienced" -> MID

    enrich_in_place(job)

    assert job.level is JobLevel.MID  # provider value preserved end-to-end


# --- join: salary (structured, minor-unit nested objects) ----------------------


def test_join_salary_survives_enrichment():
    prov = JoinProvider()
    job = prov.normalize(
        _raw(
            "join",
            {
                "id": 42,
                "idParam": "42-eng",
                "title": "Backend Engineer",
                "salaryAmountFrom": {"amount": 5_000_000, "currency": "EUR"},  # 50,000.00 EUR
                "salaryAmountTo": {"amount": 7_000_000, "currency": "EUR"},  # 70,000.00 EUR
                "salaryFrequency": "PER_YEAR",
            },
        )
    )
    assert job.salary is not None
    assert job.salary.min_amount == 50000.0
    assert job.salary.max_amount == 70000.0

    enrich_in_place(job)

    # provider-set structured salary preserved end-to-end (enrich_in_place's text-based
    # CompExtractor never runs: `if job.salary is None` guard skips it).
    assert job.salary is not None
    assert job.salary.min_amount == 50000.0
    assert job.salary.max_amount == 70000.0
    assert job.salary.currency == "EUR"


# --- breezy: salary (free-text) -------------------------------------------------


def test_breezy_salary_survives_enrichment():
    prov = BreezyProvider()
    job = prov.normalize(_raw("breezy", {"name": "Eng", "salary": "$78,000 / year"}))
    assert job.salary is not None
    assert job.salary.min_amount == 78000.0

    enrich_in_place(job)

    assert job.salary is not None
    assert job.salary.min_amount == 78000.0


# --- personio: level + years-of-experience --------------------------------------


def test_personio_level_and_years_survive_enrichment():
    prov = PersonioProvider()
    job = prov.normalize(
        _raw(
            "personio",
            {
                "id": "9001",
                "name": "Staff Systems Analyst",
                "office": "Munich",
                "seniority": "Lead",
                "yearsOfExperience": "5-10",
            },
        )
    )
    assert job.level is JobLevel.LEAD
    assert job.years_experience_min == 5
    assert job.years_experience_max == 10

    enrich_in_place(job)

    assert job.level is JobLevel.LEAD
    assert job.years_experience_min == 5
    assert job.years_experience_max == 10


# --- workable: level + degree ----------------------------------------------------


def test_workable_level_and_degree_survive_enrichment():
    prov = WorkableProvider()
    job = prov.normalize(
        _raw(
            "workable",
            {
                "shortcode": "ABCD1234",
                "title": "Director of Engineering",
                "experience": "Director",
                "education": "Bachelor's Degree",
            },
        )
    )
    assert job.level is JobLevel.DIRECTOR
    assert job.degree_min == "bachelor"

    enrich_in_place(job)

    assert job.level is JobLevel.DIRECTOR
    assert job.degree_min == "bachelor"


# --- recruitee: salary -------------------------------------------------------------


def test_recruitee_salary_survives_enrichment():
    prov = RecruiteeProvider()
    job = prov.normalize(
        _raw(
            "recruitee",
            {
                "id": 555,
                "title": "Product Manager",
                "salary": {"min": "50000", "max": "70000", "period": "year", "currency": "USD"},
            },
        )
    )
    assert job.salary is not None
    assert job.salary.min_amount == 50000.0
    assert job.salary.max_amount == 70000.0

    enrich_in_place(job)

    assert job.salary is not None
    assert job.salary.min_amount == 50000.0
    assert job.salary.max_amount == 70000.0
    assert job.salary.currency == "USD"


# --- real filter-serving path: recovered field actually changes results ------------


def test_recovered_level_changes_search_query_filter_results():
    """Drive the real serving-path filter (`SearchQuery.matches`) to prove a provider-recovered
    structured field is not just preserved in isolation but actually bites downstream: a
    strict level="senior" filter keeps the leveled job and drops the unleveled one.
    """
    # A job whose level was recovered from the ATS's own vocabulary (SmartRecruiters
    # experienceLevel), then enriched via the real serving path.
    leveled = SmartRecruitersProvider().normalize(
        _raw(
            "smartrecruiters",
            {"name": "Senior Backend Engineer", "experienceLevel": {"label": "Senior Level"}},
            source_job_id="leveled-1",
        )
    )
    enrich_in_place(leveled)
    assert leveled.level is JobLevel.SENIOR

    # A job with no ATS-provided level and a title/description carrying no seniority signal —
    # after the real enrichment pass it stays UNKNOWN.
    unleveled = JazzHRProvider().normalize(
        _raw(
            "jazzhr",
            {
                "id": "job_20260601000000_xyz789",
                "title": "Widget Assembler",
                "description": "<p>General warehouse duties.</p>",
            },
            source_job_id="unleveled-1",
        )
    )
    enrich_in_place(unleveled)
    assert unleveled.level is JobLevel.UNKNOWN

    query = SearchQuery(level=JobLevel.SENIOR, include_unknown_level=False)

    # The recovered field changes the real filter result: the provider-leveled job is kept...
    assert query.matches(leveled) is True
    # ...and the job with no recoverable level is dropped.
    assert query.matches(unleveled) is False
