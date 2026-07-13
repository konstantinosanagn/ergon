"""Tier-3 detail fetcher: WorkableProvider.fetch_detail (67,199 Workable postings, list-only).

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``. Workable's index ``apply_url`` is a BARE shortlink,
``https://apply.workable.com/j/{shortcode}`` (no account token embedded), so the real flow is
two hops: (1) a redirect-disabled GET on the shortlink whose ``Location`` header reveals the
token (``/{token}/j/{shortcode}``), then (2) a GET on the per-job JSON resource
``https://apply.workable.com/api/v1/accounts/{token}/jobs/{shortcode}``. The fake fetcher below
simulates both hops via a ``responses`` map keyed by ``(method, url)``."""
from __future__ import annotations

import anyio
import httpx

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.workable import WorkableProvider

_SHORTLINK = "https://apply.workable.com/j/516863E6FD"
_REDIRECT_TARGET = "https://apply.workable.com/jobrack/j/516863E6FD"
_JOB_API = "https://apply.workable.com/api/v1/accounts/jobrack/jobs/516863E6FD"


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_body: object = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}

    def json(self) -> object:
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


class _FakeFetcher:
    """Simulates both hops. ``redirect_location`` (if set) makes a redirect-disabled GET on the
    shortlink return a 301 with that ``Location`` header. ``job_payload`` is what the per-job
    JSON resource at ``job_url`` returns. Any URL not explicitly wired raises, so an
    unexpected extra call fails the test loudly rather than silently returning junk."""

    def __init__(
        self,
        *,
        redirect_from: str | None = None,
        redirect_location: str | None = None,
        job_url: str | None = None,
        job_payload: object = None,
        job_raises: bool = False,
    ) -> None:
        self._redirect_from = redirect_from
        self._redirect_location = redirect_location
        self._job_url = job_url
        self._job_payload = job_payload
        self._job_raises = job_raises
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.calls.append((method, url))
        if url == self._redirect_from:
            headers = (
                {} if self._redirect_location is None
                else {"location": self._redirect_location}
            )
            return _FakeResponse(status_code=301, headers=headers)
        raise AssertionError(f"unexpected request() call: {method} {url}")

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(("GET_JSON", url))
        if url == self._job_url:
            if self._job_raises:
                raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]
            return self._job_payload
        raise AssertionError(f"unexpected get_json() call: {url}")


def _job_payload(description: str = "<p>Full JD text.</p>", **extra: object) -> dict:
    payload: dict[str, object] = {"shortcode": "516863E6FD", "description": description}
    payload.update(extra)
    return payload


def test_workable_fetch_detail_two_hop_returns_description() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload=_job_payload("<p>Real JD text for the jobrack posting...</p>"),
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the jobrack posting...</p>"
    assert fetcher.calls == [
        ("GET", _SHORTLINK),
        ("GET_JSON", _JOB_API),
    ]


def test_workable_fetch_detail_concatenates_requirements_and_benefits() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload=_job_payload(
            "<p>Description.</p>", requirements="<p>Requirements.</p>", benefits="<p>Benefits.</p>"
        ),
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Description.</p>\n<p>Requirements.</p>\n<p>Benefits.</p>"


def test_workable_fetch_detail_skips_redirect_when_url_already_full() -> None:
    # apply_url already embeds the token (post-redirect / full shape) -> no redirect hop needed.
    fetcher = _FakeFetcher(job_url=_JOB_API, job_payload=_job_payload("<p>Already full.</p>"))
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_REDIRECT_TARGET, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Already full.</p>"
    assert fetcher.calls == [("GET_JSON", _JOB_API)]


def test_workable_fetch_detail_uses_ref_token_when_present() -> None:
    # ref.token set (a future board_token backfill) -> skip the redirect hop entirely.
    fetcher = _FakeFetcher(job_url=_JOB_API, job_payload=_job_payload("<p>Via ref.token.</p>"))
    ref = DetailRef(
        id="1", source="workable", token="jobrack",
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Via ref.token.</p>"
    assert fetcher.calls == [("GET_JSON", _JOB_API)]


def test_workable_fetch_detail_falls_back_to_listing_url() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload=_job_payload("<p>Fallback JD via listing_url...</p>"),
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=None, listing_url=_SHORTLINK, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD via listing_url...</p>"


def test_workable_fetch_detail_missing_description_is_none() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload={"shortcode": "516863E6FD"},
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_empty_description_is_none() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload=_job_payload("   "),
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_non_dict_payload_is_none() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload=["not", "a", "dict"],
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_non_str_description_is_none() -> None:
    # ``description`` truthy but not a string must not raise (the SmartRecruiters regression).
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_payload={"shortcode": "516863E6FD", "description": {"nested": "not-a-string"}},
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_job_fetch_failure_is_none() -> None:
    fetcher = _FakeFetcher(
        redirect_from=_SHORTLINK,
        redirect_location="/jobrack/j/516863E6FD",
        job_url=_JOB_API,
        job_raises=True,
    )
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_missing_redirect_location_is_none() -> None:
    # Redirect hop returns no Location header -> token can't be resolved -> None, never raises.
    fetcher = _FakeFetcher(redirect_from=_SHORTLINK, redirect_location=None)
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=_SHORTLINK, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_unparseable_url_is_none() -> None:
    fetcher = _FakeFetcher()
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url="https://example.com/not-a-workable-url", listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_workable_fetch_detail_no_urls_is_none() -> None:
    fetcher = _FakeFetcher()
    ref = DetailRef(
        id="1", source="workable", token=None,
        apply_url=None, listing_url=None, content_sig="s",
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher()))
    assert desc is None
