"""Providers that support cheap cross-build validation expose conditional_url(token)."""

from __future__ import annotations

from ergon_tracker.providers.ashby import AshbyProvider
from ergon_tracker.providers.base import get_provider, load_builtins
from ergon_tracker.providers.greenhouse import GreenhouseProvider
from ergon_tracker.providers.lever import LeverProvider


def test_opted_in_providers_return_exact_fetch_url():
    # Greenhouse is the one deliberate exception: its conditional_url is the LIGHT (no-content)
    # board URL, NOT fetch()'s exact ?content=true URL -- see test_greenhouse.py for the
    # raws_from_body fallback that keeps this safe (a changed-board 200 on the light URL can't
    # be silently reused; it forces a real fetch()). Every other opted-in provider below still
    # must match fetch()'s exact URL, so its stored ETag/Last-Modified validates the same payload.
    assert (
        GreenhouseProvider().conditional_url("stripe")
        == "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
    )
    assert (
        LeverProvider().conditional_url("spotify")
        == "https://api.lever.co/v0/postings/spotify?mode=json"
    )
    assert (
        AshbyProvider().conditional_url("ramp")
        == "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"
    )


def test_more_opted_in_providers_return_exact_fetch_url():
    # breezy/teamtailor/personio also honor If-None-Match -> 304 (single whole-board response).
    load_builtins()
    assert get_provider("breezy").conditional_url("acme") == "https://acme.breezy.hr/json"
    assert (
        get_provider("teamtailor").conditional_url("acme")
        == "https://acme.teamtailor.com/jobs.json"
    )
    assert get_provider("personio").conditional_url("acme") == "https://acme.jobs.personio.de/xml"


def test_paginated_or_unsupported_providers_return_none():
    # smartrecruiters paginates (page-1 ETag isn't a whole-board validator) -> must NOT opt in.
    load_builtins()
    sr = get_provider("smartrecruiters")
    assert sr is not None and sr.conditional_url("anycompany") is None
    assert sr.raws_from_body("anycompany", b"{}") is None
    rec = get_provider("recruitee")  # no validator headers
    assert rec is not None and rec.conditional_url("anycompany") is None


def test_icims_returns_none_page1_etag_is_not_a_whole_board_validator():
    # Phase-1 delta-crawl investigation (2026-07-19): both icims generations were live-probed.
    # New "Career Sites" (Jibe) JSON API (`/api/jobs?page=1&limit=100`) DOES honor conditional
    # GET -- a stored ETag reliably 304s -- but it paginates past 100 postings (e.g. real board
    # careers.amd.com: totalCount=1050, page=1 ETag != page=2 ETag), so a 304 on page 1 only
    # proves page 1 is unchanged, not the whole board. Classic gen (`/jobs/search`) sends
    # `Cache-Control: no-cache, no-store` and carries NO ETag/Last-Modified at all -- not
    # conditionally-cacheable at any granularity. Neither generation clears the
    # conditional_url contract ("validates this board's WHOLE response"), so icims must NOT
    # opt in -- see tests/live/test_conditional_get_sr_icims_live.py for the live re-verification.
    load_builtins()
    icims = get_provider("icims")
    assert icims is not None
    assert icims.conditional_url("careers.amd.com") is None
    assert icims.conditional_url("careers-winco.icims.com|classic") is None
    assert icims.raws_from_body("careers.amd.com", b"{}") is None
