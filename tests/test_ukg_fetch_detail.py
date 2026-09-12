"""Tier-3 detail fetcher: UKGProvider.fetch_detail.

Offline only. UKG's list feed carries just a short BriefDescription; the full JD lives on the
OpportunityDetail page (== ref.apply_url), embedded as a JSON ``"Description":"…"`` string. UKG's
structured pay is almost always gated (PayRangeVisible=false), but ~40-55% of postings state the
salary in the JD BODY prose -- which enrich mines once we capture it. Non-raising throughout.
"""

from __future__ import annotations

import anyio
import pytest

from ergon.enrich import enrich_in_place
from ergon.index.detail import DetailRef
from ergon.models import JobPosting
from ergon.providers.ukg import UKGProvider

_DETAIL_URL = (
    "https://recruiting.ultipro.com/ACME1000/JobBoard/"
    "9f11bf9f-0141-43d4-8b6b-7795635662ab/OpportunityDetail?opportunityId=abc"
)


class _FakeFetcher:
    def __init__(self, text: str) -> None:
        self._t = text
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        return self._t


def _ref(url: str | None = _DETAIL_URL) -> DetailRef:
    return DetailRef(
        id="1", source="ukg", token=None, apply_url=url, listing_url=None, content_sig="s"
    )


# A trimmed OpportunityDetail SPA payload: PayRangeVisible is off, but the JSON `Description`
# (escaped HTML, as UKG emits it) states the pay in prose.
_PAGE = (
    '{"PayRangeVisible":false,"Title":"Field Tech",'
    '"Description":"\\u003cp\\u003eSUMMARY: Maintain systems. '
    'Pay range is $26.44 - $31.25 per hour.\\u003c/p\\u003e"}'
)


def test_fetch_detail_extracts_full_description_body() -> None:
    fetcher = _FakeFetcher(_PAGE)
    body = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), fetcher))
    assert fetcher.calls == [_DETAIL_URL]  # fetched the OpportunityDetail page itself
    assert body is not None
    assert "<p>" in body and "$26.44 - $31.25 per hour" in body  # \uXXXX decoded


def test_fetch_detail_body_yields_prose_salary_through_enrich() -> None:
    body = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), _FakeFetcher(_PAGE)))
    job = JobPosting.create(
        source="ukg", source_job_id="1", company="", title="", description_html=body
    )
    enrich_in_place(job)
    assert job.salary is not None
    assert job.salary.min_amount == 26.44 and job.salary.max_amount == 31.25
    assert job.salary.interval.value == "hour"  # gated structured field bypassed via prose


def test_fetch_detail_non_detail_url_raises() -> None:
    # apply_url that isn't an OpportunityDetail page -> nothing to fetch -> INDETERMINATE.
    with pytest.raises(RuntimeError):
        anyio.run(
            lambda: UKGProvider().fetch_detail(
                _ref("https://recruiting.ultipro.com/x/JobBoard/g/"), _FakeFetcher(_PAGE)
            )
        )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: UKGProvider().fetch_detail(_ref(None), _FakeFetcher(_PAGE)))


def test_fetch_detail_missing_description_raises() -> None:
    for page in ('{"PayRangeVisible":false}', '{"Description":""}', "not json at all", ""):
        with pytest.raises(RuntimeError):
            anyio.run(lambda p=page: UKGProvider().fetch_detail(_ref(), _FakeFetcher(p)))


def test_fetch_detail_reads_structured_pay_range_when_visible() -> None:
    """PayRangeVisible=true -> a structured Salary, bypassing the JD-prose regex entirely."""
    from ergon.models import DetailFetch

    page = (
        '{"PayRangeVisible":true,"PayRangeMinimum":55000,"PayRangeMaximum":72000,'
        '"PayRangeCurrencyCode":"USD","Description":"\\u003cp\\u003eRole summary.\\u003c/p\\u003e"}'
    )
    res = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.salary is not None
    assert res.salary.min_amount == 55000.0 and res.salary.max_amount == 72000.0
    assert res.salary.currency == "USD"
    assert "Role summary" in res.text


def test_fetch_detail_gated_pay_range_yields_no_structured_salary() -> None:
    # PayRangeVisible=false (the common case) -> plain str, exactly as before.
    page = (
        '{"PayRangeVisible":false,"PayRangeMinimum":55000,"PayRangeMaximum":72000,'
        '"Description":"\\u003cp\\u003eRole summary.\\u003c/p\\u003e"}'
    )
    res = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert res == "<p>Role summary.</p>"


def test_fetch_detail_folds_nonempty_experience_and_education_criteria_into_text() -> None:
    """WorkExperienceCriteria/EducationCriteria (schema-confirmed, always-empty on every sample
    seen so far) fold into the JD text when a tenant DOES populate them, so the existing yoe/degree
    text extractors recover years/degree through the normal enrich pass."""
    from ergon.models import DetailFetch

    page = (
        '{"PayRangeVisible":false,'
        '"WorkExperienceCriteria":["5+ years of relevant experience required"],'
        '"EducationCriteria":["Bachelor\'s degree required"],'
        '"Description":"\\u003cp\\u003eRole summary.\\u003c/p\\u003e"}'
    )
    res = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.salary is None
    assert "5+ years of relevant experience required" in res.text
    assert "Bachelor's degree required" in res.text


def test_fetch_detail_criteria_text_recovers_years_and_degree_through_enrich() -> None:
    from ergon.enrich import enrich_in_place
    from ergon.models import JobPosting

    page = (
        '{"PayRangeVisible":false,'
        '"WorkExperienceCriteria":["5+ years of relevant experience required"],'
        '"EducationCriteria":["Bachelor\'s degree required"],'
        '"Description":"\\u003cp\\u003eRole summary.\\u003c/p\\u003e"}'
    )
    res = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, str) or res is not None
    text = res.text if hasattr(res, "text") else res
    job = JobPosting.create(
        source="ukg", source_job_id="1", company="", title="", description_html=text
    )
    enrich_in_place(job)
    assert job.years_experience_min == 5
    assert job.degree_min == "bachelor"


def test_fetch_detail_empty_criteria_arrays_yield_plain_str() -> None:
    # The universally-observed shape (both arrays present but empty) -> no text folded, no
    # DetailFetch -- byte-identical to the pre-existing behavior.
    page = (
        '{"PayRangeVisible":false,"WorkExperienceCriteria":[],"EducationCriteria":[],'
        '"Description":"\\u003cp\\u003eRole summary.\\u003c/p\\u003e"}'
    )
    res = anyio.run(lambda: UKGProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert res == "<p>Role summary.</p>"
