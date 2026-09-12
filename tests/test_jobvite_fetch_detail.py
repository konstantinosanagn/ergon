"""Tier-3 detail fetcher: JobviteProvider.fetch_detail.

Offline only. jobvite is list-only (the bulk viewall has no description/pay/date); the per-job page
(== ref.apply_url) carries an application/ld+json JobPosting whose `description` is the full JD.
jobvite doesn't disclose salary, but the body powers yoe/degree/level/skills extraction.
"""

from __future__ import annotations

import anyio
import httpx
import pytest

from ergon.enrich import enrich_in_place
from ergon.index.detail import DetailRef
from ergon.models import JobPosting
from ergon.providers.jobvite import JobviteProvider

# fetch_detail's contract: an INDETERMINATE condition raises rather than returning None,
# because a returned None expires a live index row. These are the shapes it may raise as.
_INDETERMINATE = (RuntimeError, httpx.HTTPError, OSError, ValueError)

_URL = "https://jobs.jobvite.com/acme/job/oABC123"


# fetch_detail's contract: an INDETERMINATE condition raises rather than returning None, because
# a returned None expires a live index row. These are the shapes it may raise as.


class _FakeFetcher:
    def __init__(self, text: str) -> None:
        self._t = text
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        return self._t


def _ref(url: str | None = _URL) -> DetailRef:
    return DetailRef(
        id="1", source="jobvite", token=None, apply_url=url, listing_url=None, content_sig="s"
    )


_PAGE = (
    "<html><head>"
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"JobPosting","title":"Data Engineer",'
    '"description":"\\u003cp\\u003eRequires a Bachelor\\u0027s degree and 5+ years of experience '
    'building data pipelines.\\u003c/p\\u003e"}'
    "</script></head><body>...</body></html>"
)


def test_fetch_detail_returns_jsonld_description() -> None:
    fetcher = _FakeFetcher(_PAGE)
    body = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), fetcher))
    assert fetcher.calls == [_URL]  # fetched the per-job page itself
    assert body is not None
    assert "Bachelor" in body and "5+ years" in body


def test_fetch_detail_body_yields_yoe_and_degree_through_enrich() -> None:
    body = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(_PAGE)))
    job = JobPosting.create(
        source="jobvite", source_job_id="1", company="", title="", description_html=body
    )
    enrich_in_place(job)
    # salary is absent (jobvite doesn't disclose), but the body still powers the other extractors
    assert job.years_experience_min == 5
    assert job.degree_min == "bachelor"


def test_fetch_detail_missing_url_or_jsonld_raises() -> None:
    """Indeterminate inputs must RAISE, never return None — None expires a live index row."""
    with pytest.raises(_INDETERMINATE):
        anyio.run(lambda: JobviteProvider().fetch_detail(_ref(None), _FakeFetcher(_PAGE)))
    for page in ("<html><body>no json-ld</body></html>", "", "not html"):
        with pytest.raises(_INDETERMINATE):
            anyio.run(lambda p=page: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(p)))


def test_fetch_detail_returns_structured_locations() -> None:
    from ergon.models import DetailFetch

    page = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"\\u003cp\\u003eRole.\\u003c/p\\u003e",'
        '"jobLocation":[{"@type":"Place","address":{"addressLocality":"Phoenix",'
        '"addressRegion":"Arizona","addressCountry":"United States"}},'
        '{"@type":"Place","address":{"addressCountry":"United States"}}]}'
        "</script></head><body></body></html>"
    )
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.locations and res.locations[0].city == "Phoenix"
    assert res.locations[0].region == "Arizona" and res.locations[0].country == "United States"


def test_jsonld_locations_parses_single_and_list_and_skips_empty() -> None:
    P = JobviteProvider.jsonld_locations
    assert P(None) == []
    assert P({"address": {"addressCountry": "United States"}})[0].country == "United States"
    # bare place with no usable address field is skipped
    assert P([{"@type": "Place"}, {"address": {}}]) == []


def test_fetch_detail_reads_employment_type_salary_and_folds_education_text() -> None:
    """The audit's quick-win: employmentType/baseSalary/educationRequirements are standard
    sibling properties on the same JSON-LD JobPosting object already parsed for description."""
    from ergon.models import DetailFetch, EmploymentType, SalaryInterval

    page = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"\\u003cp\\u003eRole.\\u003c/p\\u003e",'
        '"employmentType":"FULL_TIME",'
        '"baseSalary":{"@type":"MonetaryAmount","currency":"USD","value":{'
        '"@type":"QuantitativeValue","minValue":90000,"maxValue":120000,"unitText":"YEAR"}},'
        '"educationRequirements":"Bachelor\'s degree required"}'
        "</script></head><body></body></html>"
    )
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.employment_type is EmploymentType.FULL_TIME
    assert res.salary is not None
    assert res.salary.min_amount == 90000.0 and res.salary.max_amount == 120000.0
    assert res.salary.interval is SalaryInterval.YEAR
    assert "Education requirements: Bachelor's degree required." in res.text


def test_fetch_detail_education_requirements_object_shape() -> None:
    # Newer schema.org shape: educationRequirements as an EducationalOccupationalCredential.
    page = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"\\u003cp\\u003eRole.\\u003c/p\\u003e",'
        '"educationRequirements":{"@type":"EducationalOccupationalCredential",'
        '"credentialCategory":"bachelor degree"}}'
        "</script></head><body></body></html>"
    )
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    text = res.text if hasattr(res, "text") else res
    assert "bachelor degree" in text


def test_fetch_detail_education_text_recovers_degree_min_through_enrich() -> None:
    page = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"\\u003cp\\u003eRole summary.\\u003c/p\\u003e",'
        '"educationRequirements":"Bachelor\'s degree required"}'
        "</script></head><body></body></html>"
    )
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    text = res.text if hasattr(res, "text") else res
    job = JobPosting.create(
        source="jobvite", source_job_id="1", company="", title="", description_html=text
    )
    enrich_in_place(job)
    assert job.degree_min == "bachelor"


def test_fetch_detail_no_standard_properties_yields_plain_str() -> None:
    # No employmentType/baseSalary/educationRequirements/jobLocation on the page -> unchanged
    # pre-existing behavior (a bare str, not a DetailFetch).
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(_PAGE)))
    assert isinstance(res, str)


def test_fetch_detail_maps_education_vocabulary_to_degree_min() -> None:
    """A closed-vocabulary ``educationRequirements`` now rides back STRUCTURED on
    ``DetailFetch.degree_min`` (the reconcile seeds it, so it beats text extraction); the text
    fold stays in place alongside it."""
    from ergon.models import DetailFetch

    page = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"\\u003cp\\u003eRole.\\u003c/p\\u003e",'
        '"educationRequirements":{"@type":"EducationalOccupationalCredential",'
        '"credentialCategory":"Master\'s Degree"}}'
        "</script></head><body></body></html>"
    )
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.degree_min == "master"
    assert "Education requirements: Master's Degree." in res.text  # fold kept as the fallback


def test_fetch_detail_unmappable_education_leaves_degree_min_none() -> None:
    """Free prose (and the deliberately ambiguous ATS values) must NOT be guessed at here — the
    text fold is what gives the description extractor its shot."""
    from ergon.models import DetailFetch

    for edu in ("Bachelor's degree in Computer Science or equivalent", "Professional"):
        page = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","description":"\\u003cp\\u003eRole.\\u003c/p\\u003e",'
            '"educationRequirements":"' + edu + '"}'
            "</script></head><body></body></html>"
        )
        res = anyio.run(lambda p=page: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(p)))
        assert isinstance(res, DetailFetch)
        assert res.degree_min is None
        assert "Education requirements:" in res.text


def test_fetch_detail_no_education_leaves_degree_min_none() -> None:
    from ergon.models import DetailFetch

    page = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","description":"\\u003cp\\u003eRole.\\u003c/p\\u003e",'
        '"employmentType":"FULL_TIME"}'
        "</script></head><body></body></html>"
    )
    res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.degree_min is None
