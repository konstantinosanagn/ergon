"""Tier-3 detail fetcher: JobviteProvider.fetch_detail.

Offline only. jobvite is list-only (the bulk viewall has no description/pay/date); the per-job page
(== ref.apply_url) carries an application/ld+json JobPosting whose `description` is the full JD.
jobvite doesn't disclose salary, but the body powers yoe/degree/level/skills extraction.
"""

from __future__ import annotations

import anyio

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import JobPosting
from ergon_tracker.providers.jobvite import JobviteProvider

_URL = "https://jobs.jobvite.com/acme/job/oABC123"


class _FakeFetcher:
    def __init__(self, text: str) -> None:
        self._t = text
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        return self._t


def _ref(url: str | None = _URL) -> DetailRef:
    return DetailRef(id="1", source="jobvite", token=None, apply_url=url, listing_url=None, content_sig="s")


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
    job = JobPosting.create(source="jobvite", source_job_id="1", company="", title="", description_html=body)
    enrich_in_place(job)
    # salary is absent (jobvite doesn't disclose), but the body still powers the other extractors
    assert job.years_experience_min == 5
    assert job.degree_min == "bachelor"


def test_fetch_detail_missing_url_or_jsonld_returns_none() -> None:
    assert anyio.run(lambda: JobviteProvider().fetch_detail(_ref(None), _FakeFetcher(_PAGE))) is None
    for page in ("<html><body>no json-ld</body></html>", "", "not html"):
        res = anyio.run(lambda: JobviteProvider().fetch_detail(_ref(), _FakeFetcher(page)))
        assert res is None


def test_fetch_detail_returns_structured_locations() -> None:
    from ergon_tracker.models import DetailFetch

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
    P = JobviteProvider._jsonld_locations
    assert P(None) == []
    assert P({"address": {"addressCountry": "United States"}})[0].country == "United States"
    # bare place with no usable address field is skipped
    assert P([{"@type": "Place"}, {"address": {}}]) == []
