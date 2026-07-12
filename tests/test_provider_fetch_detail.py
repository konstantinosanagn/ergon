"""Tier-3 detail fetcher: fetch_detail contract (base) + SmartRecruiters implementation.

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload

    async def get_json(self, url: str, **kw: object) -> object:
        return self._p


def _sr_payload(job_description: str = "<p>Full JD. 5+ years. Bachelor's required.</p>",
                 qualifications: str | None = "<p>BS in CS or equivalent.</p>") -> dict:
    sections: dict = {"jobDescription": {"text": job_description}}
    if qualifications is not None:
        sections["qualifications"] = {"text": qualifications}
    return {"jobAd": {"sections": sections}}


def test_smartrecruiters_fetch_detail_returns_description() -> None:
    payload = _sr_payload()
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token="acme",
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(
        lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload))
    )
    assert desc is not None
    assert "Full JD" in desc
    assert "BS in CS" in desc


def test_smartrecruiters_fetch_detail_missing_job_description_is_none() -> None:
    payload = {"jobAd": {"sections": {"qualifications": {"text": "BS required"}}}}
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token="acme",
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(
        lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload))
    )
    assert desc is None


def test_smartrecruiters_fetch_detail_unparseable_apply_url_is_none() -> None:
    payload = _sr_payload()
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token=None,
        apply_url="https://example.com/not-a-smartrecruiters-url",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(
        lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload))
    )
    assert desc is None


def test_smartrecruiters_fetch_detail_no_urls_is_none() -> None:
    payload = _sr_payload()
    ref = DetailRef(
        id="1", source="smartrecruiters", token=None, apply_url=None, listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(
        lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload))
    )
    assert desc is None


def test_smartrecruiters_fetch_detail_derives_token_from_apply_url_when_ref_token_missing() -> None:
    payload = _sr_payload()
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token=None,
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(
        lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload))
    )
    assert desc is not None
    assert "Full JD" in desc


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None
