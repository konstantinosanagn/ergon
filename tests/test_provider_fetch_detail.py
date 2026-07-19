"""Tier-3 detail fetcher: fetch_detail contract (base) + SmartRecruiters implementation.

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls."""

from __future__ import annotations

import anyio
import pytest

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider, get_provider, load_builtins
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload

    async def get_json(self, url: str, **kw: object) -> object:
        return self._p


def _sr_payload(
    job_description: str = "<p>Full JD. 5+ years. Bachelor's required.</p>",
    qualifications: str | None = "<p>BS in CS or equivalent.</p>",
) -> dict:
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
    desc = anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is not None
    assert "Full JD" in desc
    assert "BS in CS" in desc


def test_smartrecruiters_fetch_detail_recovers_from_secondary_sections() -> None:
    # A missing/empty jobDescription must NOT drop the posting when other JD sections carry text:
    # measured ~40% of failed SR postings have their real content only in qualifications/
    # additionalInformation. (Previously this returned None -- the dropped-content bug.)
    payload = {"jobAd": {"sections": {"qualifications": {"text": "BS required"}}}}
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token="acme",
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc == "BS required"


def test_smartrecruiters_fetch_detail_raises_when_no_section_has_text() -> None:
    # Only when EVERY JD-relevant section is empty does fetch_detail raise.
    payload = {
        "jobAd": {"sections": {"jobDescription": {"text": ""}, "qualifications": {"text": ""}}}
    }
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token="acme",
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_smartrecruiters_fetch_detail_unparseable_apply_url_raises() -> None:
    payload = _sr_payload()
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token=None,
        apply_url="https://example.com/not-a-smartrecruiters-url",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_smartrecruiters_fetch_detail_no_urls_raises() -> None:
    payload = _sr_payload()
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token=None,
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_smartrecruiters_fetch_detail_non_dict_job_ad_raises() -> None:
    # ``jobAd`` truthy but not a dict (e.g. an unexpected string shape) is INDETERMINATE.
    payload = {"jobAd": "unexpected-string"}
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token="acme",
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_smartrecruiters_fetch_detail_non_dict_job_description_raises() -> None:
    # ``jobDescription`` truthy but not a dict is INDETERMINATE.
    payload = {"jobAd": {"sections": {"jobDescription": "unexpected-string"}}}
    ref = DetailRef(
        id="1",
        source="smartrecruiters",
        token="acme",
        apply_url="https://jobs.smartrecruiters.com/acme/743999983512345",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))


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
    desc = anyio.run(lambda: SmartRecruitersProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is not None
    assert "Full JD" in desc


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(
        id="1", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None


# --- registry-glue dispatch (build_index._reconcile_detail's real path): DetailRef.from_row ->
# get_provider(ref.source) -> provider.fetch_detail(ref, fetcher). Each half is tested elsewhere
# in isolation (DetailRef.from_row above, SmartRecruitersProvider.fetch_detail above/via a fake
# fetcher) but never wired together through the real registry + a real AsyncFetcher -- this proves
# the seam. Only `AsyncFetcher.get_json` is stubbed; everything else (from_row, get_provider,
# fetch_detail) is the real production code path.


def test_dispatch_glue_from_row_through_registry_to_real_fetch_detail(monkeypatch) -> None:
    payload = {
        "jobAd": {
            "sections": {
                "jobDescription": {"text": "Full JD text for the dispatch-glue test."},
                "qualifications": {"text": "BS in CS or equivalent."},
            }
        }
    }

    async def fake_get_json(self, url, **kwargs):  # noqa: ANN001, ARG001
        return payload

    monkeypatch.setattr(AsyncFetcher, "get_json", fake_get_json)

    # A smartrecruiters-shaped index row, as `_tier3_rows` would select it.
    row = {
        "id": "1",
        "source": "smartrecruiters",
        "board_token": "acme",
        "apply_url": "https://jobs.smartrecruiters.com/acme/743999983512345",
        "listing_url": None,
        "content_hash": "h1",
    }
    ref = DetailRef.from_row(row)
    assert ref.source == "smartrecruiters" and ref.token == "acme"

    load_builtins()
    provider = get_provider(ref.source)
    assert isinstance(provider, SmartRecruitersProvider)  # real registry resolution, not a fake

    async def _run():
        async with AsyncFetcher() as fetcher:
            return await provider.fetch_detail(ref, fetcher)

    desc = anyio.run(_run)
    assert desc is not None
    assert "Full JD text for the dispatch-glue test." in desc
    assert "BS in CS" in desc
