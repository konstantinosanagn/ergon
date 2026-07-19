"""Tier-3 detail fetcher: RadancyProvider.fetch_detail.

Offline only -- a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``. Recon (7/7 live tenants) verified the Radancy
``apply_url``/``listing_url`` IS ALREADY the full CMS-rendered job detail page, but that a
per-tenant ``div.job-description`` selector is unreliable: on ~4/7 tenants the first match is a
short 62-172 char meta/summary chip, not the JD body. These tests cover the selector-chain +
length-threshold + whole-page-fallback logic that guards against that."""

from __future__ import annotations

import anyio
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.radancy import RadancyProvider


class _FakeFetcher:
    def __init__(self, payload: object, *, raise_exc: bool = False) -> None:
        self._p = payload
        self._raise = raise_exc
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        if self._raise:
            raise RuntimeError("boom")
        return self._p  # type: ignore[return-value]


_APPLY_URL = "https://jobs.carnival.com/job/davie/some-title/8858/97622797120"


def _ref(*, apply_url: str | None = _APPLY_URL, listing_url: str | None = None) -> DetailRef:
    return DetailRef(
        id="1",
        source="radancy",
        token="jobs.carnival.com|Carnival",
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def test_radancy_fetch_detail_returns_real_job_description_body() -> None:
    # A real ``div.job-description`` >= 400 chars is returned as-is (not the whole page).
    long_body = "<p>" + ("Full JD text describing the role and responsibilities. " * 10) + "</p>"
    assert len(long_body) >= 400
    html = (
        "<html><body><nav>Home About</nav>"
        f'<div class="job-description">{long_body}</div>'
        "<footer>copyright</footer></body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = _ref()
    desc = anyio.run(lambda: RadancyProvider().fetch_detail(ref, fetcher))
    assert desc is not None
    assert "Full JD text describing the role" in desc
    assert "copyright" not in desc  # proves it returned the container, not the whole page
    assert fetcher.calls == [_APPLY_URL]


def test_radancy_fetch_detail_falls_back_to_whole_page_when_chip_is_short() -> None:
    # ``div.job-description`` is a short meta/summary chip (< 400 chars) -- recon's ~4/7 case.
    # The real JD text lives elsewhere on the page (e.g. inside a generic wrapper the selector
    # chain doesn't match), so the whole-page fallback must win, returning the LONG text.
    short_chip = "Senior Engineer - Miami, FL"  # well under 400 chars
    assert len(short_chip) < 400
    real_jd = "Full job description with responsibilities and requirements. " * 10
    assert len(real_jd) >= 400
    html = (
        "<html><body>"
        f'<div class="job-description">{short_chip}</div>'
        f'<div class="content-wrapper"><p>{real_jd}</p></div>'
        "</body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = _ref()
    desc = anyio.run(lambda: RadancyProvider().fetch_detail(ref, fetcher))
    assert desc is not None
    # The whole-page fallback won -- the long JD text is present (not just the short chip alone).
    assert "Full job description with responsibilities" in desc
    assert len(desc) >= 400
    assert fetcher.calls == [_APPLY_URL]


def test_radancy_fetch_detail_empty_page_raises() -> None:
    fetcher = _FakeFetcher("   ")
    ref = _ref()
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RadancyProvider().fetch_detail(ref, fetcher))


def test_radancy_fetch_detail_no_urls_raises() -> None:
    fetcher = _FakeFetcher("<html><body>irrelevant</body></html>")
    ref = _ref(apply_url=None, listing_url=None)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RadancyProvider().fetch_detail(ref, fetcher))
    assert fetcher.calls == []  # no fetch attempted when neither url is present


def test_radancy_fetch_detail_fetch_raises_propagates() -> None:
    fetcher = _FakeFetcher("<html></html>", raise_exc=True)
    ref = _ref()
    with pytest.raises(RuntimeError, match="boom"):
        anyio.run(lambda: RadancyProvider().fetch_detail(ref, fetcher))


def test_radancy_fetch_detail_falls_back_to_listing_url() -> None:
    long_body = "<p>" + ("Fallback listing_url JD text. " * 15) + "</p>"
    assert len(long_body) >= 400
    html = f'<html><body><div class="job-description">{long_body}</div></body></html>'
    fetcher = _FakeFetcher(html)
    listing_url = "https://jobs.carnival.com/job/miami/some-title/1234/55555555555"
    ref = _ref(apply_url=None, listing_url=listing_url)
    desc = anyio.run(lambda: RadancyProvider().fetch_detail(ref, fetcher))
    assert desc is not None
    assert "Fallback listing_url JD text" in desc
    assert fetcher.calls == [listing_url]


def test_base_fetch_detail_is_none() -> None:
    ref = _ref()
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher("<html></html>")))
    assert desc is None


def test_fetch_detail_recovers_jsonld_location() -> None:
    from ergon_tracker.models import DetailFetch

    page = (
        "<html><body><div class='job-description'>" + ("Full JD body. " * 40) + "</div>"
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","jobLocation":[{"@type":"Place","address":{'
        '"addressLocality":"Eden Prairie","addressRegion":"Minnesota",'
        '"addressCountry":"United States"}}]}</script></body></html>'
    )
    res = anyio.run(lambda: RadancyProvider().fetch_detail(_ref(), _FakeFetcher(page)))
    assert isinstance(res, DetailFetch)
    assert res.locations[0].city == "Eden Prairie" and res.locations[0].country == "United States"
