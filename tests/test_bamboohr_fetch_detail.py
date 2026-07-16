"""Tier-3 detail fetcher: BambooHRProvider.fetch_detail.

Offline only — a FakeFetcher stands in for AsyncFetcher. The list feed is thin (no pay/desc); the
``/careers/{id}/detail`` endpoint returns ``result.jobOpening`` with a description + a free-text
``compensation`` string that we parse into a structured salary and return via ``DetailFetch``.
Non-raising: any unparseable ref, fetch failure, non-dict payload, or empty description -> ``None``.
"""

from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef, _detail_parts
from ergon_tracker.models import DetailFetch, SalaryInterval
from ergon_tracker.providers.bamboohr import BambooHRProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._p


def _ref() -> DetailRef:
    return DetailRef(
        id="109",
        source="bamboohr",
        token="evergreene",
        apply_url="https://evergreene.bamboohr.com/careers/109",
        listing_url=None,
        content_sig="s",
    )


def test_fetch_detail_returns_body_plus_structured_salary() -> None:
    payload = {
        "result": {
            "jobOpening": {
                "description": "<p>Build historic restorations.</p>",
                "compensation": "$85K - 135K Base per year DOE",
            }
        }
    }
    fetcher = _FakeFetcher(payload)
    res = anyio.run(lambda: BambooHRProvider().fetch_detail(_ref(), fetcher))
    assert fetcher.calls == ["https://evergreene.bamboohr.com/careers/109/detail"]
    assert isinstance(res, DetailFetch)
    text, sal, _locs = _detail_parts(res)
    assert text == "<p>Build historic restorations.</p>"
    assert sal is not None
    assert sal.min_amount == 85000 and sal.max_amount == 135000
    assert sal.currency == "USD" and sal.interval is SalaryInterval.YEAR


def test_fetch_detail_no_compensation_returns_bare_str() -> None:
    # No compensation -> bare str (enrich still body-extracts), same as any other Tier-3 provider.
    payload = {"result": {"jobOpening": {"description": "<p>No pay stated.</p>"}}}
    res = anyio.run(lambda: BambooHRProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert res == "<p>No pay stated.</p>"


def test_fetch_detail_empty_description_returns_none() -> None:
    payload = {"result": {"jobOpening": {"compensation": "$85K per year", "description": "  "}}}
    assert anyio.run(lambda: BambooHRProvider().fetch_detail(_ref(), _FakeFetcher(payload))) is None


def test_fetch_detail_non_dict_payload_returns_none() -> None:
    for bad in (None, [], "nope", {"result": None}, {"result": {"jobOpening": "x"}}):
        res = anyio.run(lambda b=bad: BambooHRProvider().fetch_detail(_ref(), _FakeFetcher(b)))
        assert res is None


def test_parse_detail_ref_from_url_and_token_fallback() -> None:
    assert BambooHRProvider._parse_detail_ref(_ref()) == ("evergreene", "109")
    # no url -> fall back to ref.token / ref.id
    ref2 = DetailRef(
        id="42", source="bamboohr", token="acme", apply_url=None, listing_url=None, content_sig="s"
    )
    assert BambooHRProvider._parse_detail_ref(ref2) == ("acme", "42")
    ref3 = DetailRef(
        id="", source="bamboohr", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    assert BambooHRProvider._parse_detail_ref(ref3) is None


def test_fetch_detail_recovers_structured_location() -> None:
    from ergon_tracker.models import DetailFetch

    payload = {
        "result": {
            "jobOpening": {
                "description": "<p>Role.</p>",
                "location": {
                    "city": "Brooklyn",
                    "state": "New York",
                    "addressCountry": "United States",
                },
            }
        }
    }
    res = anyio.run(lambda: BambooHRProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert isinstance(res, DetailFetch)
    assert res.locations and res.locations[0].city == "Brooklyn"
    assert res.locations[0].region == "New York" and res.locations[0].country == "United States"


def test_detail_location_prefers_location_then_atslocation_else_empty() -> None:
    P = BambooHRProvider._detail_location
    assert (
        P({"atsLocation": {"city": "Austin", "state": "TX", "country": "United States"}})[0].city
        == "Austin"
    )
    # location (with addressCountry) wins over atsLocation
    both = P(
        {
            "location": {"city": "Reno", "addressCountry": "United States"},
            "atsLocation": {"city": "X", "country": "Y"},
        }
    )
    assert both[0].city == "Reno" and both[0].country == "United States"
    assert P({"location": {"city": None}, "atsLocation": {}}) == []
