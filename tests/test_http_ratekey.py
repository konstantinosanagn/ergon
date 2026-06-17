"""Rate-limit key: shared backends collapse to the registrable domain; Workday stays per-host."""

from __future__ import annotations

from ergon_tracker.http import _rate_key


def test_shared_backend_subdomains_collapse() -> None:
    assert _rate_key("channable.recruitee.com") == "recruitee.com"
    assert _rate_key("foo.recruitee.com") == "recruitee.com"
    assert _rate_key("acme.jobs.personio.de") == "personio.de"


def test_workday_stays_per_tenant() -> None:
    assert _rate_key("nvidia.wd5.myworkdayjobs.com") == "nvidia.wd5.myworkdayjobs.com"
    assert _rate_key("salesforce.wd12.myworkdayjobs.com") == "salesforce.wd12.myworkdayjobs.com"


def test_single_host_providers_unchanged() -> None:
    assert _rate_key("boards-api.greenhouse.io") == "greenhouse.io"
    assert _rate_key("api.lever.co") == "lever.co"
    assert _rate_key("remoteok.com") == "remoteok.com"


def test_two_level_tld() -> None:
    assert _rate_key("acme.co.uk") == "acme.co.uk"
    assert _rate_key("jobs.acme.co.uk") == "acme.co.uk"
