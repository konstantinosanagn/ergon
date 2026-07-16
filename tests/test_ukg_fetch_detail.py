"""Tier-3 detail fetcher: UKGProvider.fetch_detail.

Offline only. UKG's list feed carries just a short BriefDescription; the full JD lives on the
OpportunityDetail page (== ref.apply_url), embedded as a JSON ``"Description":"…"`` string. UKG's
structured pay is almost always gated (PayRangeVisible=false), but ~40-55% of postings state the
salary in the JD BODY prose -- which enrich mines once we capture it. Non-raising throughout.
"""

from __future__ import annotations

import anyio

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import JobPosting
from ergon_tracker.providers.ukg import UKGProvider

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


def test_fetch_detail_non_detail_url_returns_none() -> None:
    # apply_url that isn't an OpportunityDetail page -> nothing to fetch.
    res = anyio.run(
        lambda: UKGProvider().fetch_detail(
            _ref("https://recruiting.ultipro.com/x/JobBoard/g/"), _FakeFetcher(_PAGE)
        )
    )
    assert res is None
    assert anyio.run(lambda: UKGProvider().fetch_detail(_ref(None), _FakeFetcher(_PAGE))) is None


def test_fetch_detail_missing_description_returns_none() -> None:
    for page in ('{"PayRangeVisible":false}', '{"Description":""}', "not json at all", ""):
        res = anyio.run(lambda p=page: UKGProvider().fetch_detail(_ref(), _FakeFetcher(p)))
        assert res is None
