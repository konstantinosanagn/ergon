"""Tier-3 detail fetcher: PhenomProvider.fetch_detail is a RE-ROUTE dispatcher, not a
phenom-native fetcher. 11,414 of 11,831 phenom rows are AGGREGATED listings whose
``apply_url`` points at Workday (11,083) or SuccessFactors (331) -- ATSes we already have
working ``fetch_detail`` for. This dispatches by the ``apply_url`` (falling back to
``listing_url``) host to the matching provider, recovering 96.5% of phenom rows for free.

Offline only -- a fake fetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``'s fake-fetcher style, but serves BOTH the Workday cxs
JSON shape (``get_json``) and the SuccessFactors HTML shape (``get_text``) so one fetcher can
back both delegate paths."""

from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.phenom import PhenomProvider


class _FakeFetcher:
    """Serves both Workday's cxs JSON (get_json) and SuccessFactors' HTML (get_text)."""

    def __init__(
        self,
        json_payload: object | None = None,
        html_payload: str | None = None,
        raise_on: str | None = None,
    ) -> None:
        self._json = json_payload
        self._html = html_payload
        self._raise_on = raise_on
        self.get_json_calls: list[str] = []
        self.get_text_calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.get_json_calls.append(url)
        if self._raise_on == "get_json":
            raise RuntimeError("boom")
        return self._json

    async def get_text(self, url: str, **kw: object) -> str:
        self.get_text_calls.append(url)
        if self._raise_on == "get_text":
            raise RuntimeError("boom")
        return self._html or ""


_WD_JSON = {"jobPostingInfo": {"jobDescription": "<p>WD JD</p>"}}
_SF_HTML = '<div id="jobdescription"><p>SF JD</p></div>'


def test_phenom_workday_apply_url_strips_trailing_apply_and_delegates() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON)
    ref = DetailRef(
        id="1",
        source="phenom",
        token="careers.example.com",
        apply_url=(
            "https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100/apply"
        ),
        listing_url="https://careers.example.com/job/12345",
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>WD JD</p>"
    # Proves the "/apply" suffix was stripped BEFORE building the cxs URL.
    assert fetcher.get_json_calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_careers/job/"
        "USA-Remote/Engineer_R-100"
    ]


def test_phenom_workday_apply_url_without_trailing_apply_still_works() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON)
    ref = DetailRef(
        id="2",
        source="phenom",
        token="careers.example.com",
        apply_url="https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>WD JD</p>"
    assert fetcher.get_json_calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_careers/job/"
        "USA-Remote/Engineer_R-100"
    ]


def test_phenom_workday_falls_back_to_listing_url() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON)
    ref = DetailRef(
        id="3",
        source="phenom",
        token="careers.example.com",
        apply_url=None,
        listing_url=(
            "https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100/apply"
        ),
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>WD JD</p>"
    assert fetcher.get_json_calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_careers/job/"
        "USA-Remote/Engineer_R-100"
    ]


def test_phenom_successfactors_apply_url_delegates() -> None:
    fetcher = _FakeFetcher(html_payload=_SF_HTML)
    ref = DetailRef(
        id="4",
        source="phenom",
        token="careers.example.com",
        apply_url="https://career8.successfactors.com/career?company=acme&job=12345678",
        listing_url="https://careers.example.com/job/999",
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == _SF_HTML
    # SF re-GETs the stored apply_url verbatim -- no URL rewriting for this path.
    assert fetcher.get_text_calls == [
        "https://career8.successfactors.com/career?company=acme&job=12345678"
    ]


def test_phenom_sapsf_host_delegates_to_successfactors() -> None:
    fetcher = _FakeFetcher(html_payload=_SF_HTML)
    ref = DetailRef(
        id="5",
        source="phenom",
        token="careers.example.com",
        apply_url="https://performancemanager.sapsf.com/sap/job/Analyst/12345678/",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == _SF_HTML


def test_phenom_genuine_phenom_host_is_none() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON, html_payload=_SF_HTML)
    ref = DetailRef(
        id="6",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/job/123",
        listing_url="https://careers.molsoncoors.com/job/123",
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.get_json_calls == []
    assert fetcher.get_text_calls == []


def test_phenom_both_urls_none_is_none() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON, html_payload=_SF_HTML)
    ref = DetailRef(
        id="7",
        source="phenom",
        token="careers.example.com",
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.get_json_calls == []
    assert fetcher.get_text_calls == []


def test_phenom_workday_delegate_raises_is_none() -> None:
    fetcher = _FakeFetcher(raise_on="get_json")
    ref = DetailRef(
        id="8",
        source="phenom",
        token="careers.example.com",
        apply_url=(
            "https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100/apply"
        ),
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_phenom_successfactors_delegate_raises_is_none() -> None:
    fetcher = _FakeFetcher(raise_on="get_text")
    ref = DetailRef(
        id="9",
        source="phenom",
        token="careers.example.com",
        apply_url="https://career8.successfactors.com/career?company=acme&job=12345678",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    """The generic BaseProvider default (no per-posting detail endpoint) -- mirrors
    ``tests/test_workday_fetch_detail.py``'s equivalent check."""
    ref = DetailRef(
        id="10", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher()))
    assert desc is None
