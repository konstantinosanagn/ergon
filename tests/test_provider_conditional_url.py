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
    rec = get_provider("recruitee")  # no validator headers
    assert rec is not None and rec.conditional_url("anycompany") is None
