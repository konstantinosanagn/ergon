"""Tier-3 detail fetcher: SuccessFactorsProvider.fetch_detail.

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``: SuccessFactors has no separate detail API, so
``fetch_detail`` simply re-fetches the URL already stored on the posting (``ref.apply_url``,
falling back to ``ref.listing_url``) and pulls the JD out of ``#jobdescription`` (falling back
to the class-only ``.jobdescription``)."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.successfactors import SuccessFactorsProvider


class _FakeFetcher:
    def __init__(self, html: object) -> None:
        self._html = html
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        if isinstance(self._html, Exception):
            raise self._html
        return self._html


_APPLY_URL = "https://careers.ey.com/ey/job/Analyst-BLR-560103/12345678/"
_LISTING_URL = "https://careers.ey.com/ey/job/Analyst-BLR-560103-alt/87654321/"


def _page(inner: str) -> str:
    return f"<html><body><h1>Analyst</h1>{inner}<footer>Apply now</footer></body></html>"


def test_returns_jd_html_and_calls_apply_url() -> None:
    html = _page('<div id="jobdescription"><p>JD text...</p></div>')
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="1",
        source="successfactors",
        token=None,
        apply_url=_APPLY_URL,
        listing_url=_LISTING_URL,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc == '<div id="jobdescription"><p>JD text...</p></div>'
    assert fetcher.calls == [_APPLY_URL]


def test_falls_back_to_class_only_jobdescription() -> None:
    html = _page('<div class="jobdescription"><p>Class-only JD...</p></div>')
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="2",
        source="successfactors",
        token=None,
        apply_url=_APPLY_URL,
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc == '<div class="jobdescription"><p>Class-only JD...</p></div>'
    assert fetcher.calls == [_APPLY_URL]


def test_no_jobdescription_node_is_none() -> None:
    html = _page('<div class="other">Nothing relevant here</div>')
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="3",
        source="successfactors",
        token=None,
        apply_url=_APPLY_URL,
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_empty_jobdescription_node_is_none() -> None:
    html = _page('<div id="jobdescription"></div>')
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="4",
        source="successfactors",
        token=None,
        apply_url=_APPLY_URL,
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_no_urls_is_none_and_no_fetch_attempted() -> None:
    fetcher = _FakeFetcher(_page('<div id="jobdescription"><p>unused</p></div>'))
    ref = DetailRef(
        id="5",
        source="successfactors",
        token=None,
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_fetch_raising_is_none() -> None:
    fetcher = _FakeFetcher(RuntimeError("boom"))
    ref = DetailRef(
        id="6",
        source="successfactors",
        token=None,
        apply_url=_APPLY_URL,
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_falls_back_to_listing_url_when_apply_url_missing() -> None:
    html = _page('<div id="jobdescription"><p>Via listing_url...</p></div>')
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="7",
        source="successfactors",
        token=None,
        apply_url=None,
        listing_url=_LISTING_URL,
        content_sig="s",
    )
    desc = anyio.run(lambda: SuccessFactorsProvider().fetch_detail(ref, fetcher))
    assert desc == '<div id="jobdescription"><p>Via listing_url...</p></div>'
    assert fetcher.calls == [_LISTING_URL]


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher("<html></html>")))
    assert desc is None
