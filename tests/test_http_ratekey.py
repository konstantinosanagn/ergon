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


def test_oracle_and_icims_stay_per_tenant() -> None:
    # Each Oracle/iCIMS tenant is a separate customer's careers site on separate infra (same
    # shape as Workday) -> collapsing to the registrable domain is a false bottleneck.
    assert _rate_key("ehac.fa.us6.oraclecloud.com") == "ehac.fa.us6.oraclecloud.com"
    assert _rate_key("eeho.fa.us2.oraclecloud.com") == "eeho.fa.us2.oraclecloud.com"
    assert _rate_key("careers-costco.icims.com") == "careers-costco.icims.com"


def test_single_host_providers_unchanged() -> None:
    assert _rate_key("boards-api.greenhouse.io") == "greenhouse.io"
    assert _rate_key("api.lever.co") == "lever.co"
    assert _rate_key("remoteok.com") == "remoteok.com"


def test_two_level_tld() -> None:
    assert _rate_key("acme.co.uk") == "acme.co.uk"
    assert _rate_key("jobs.acme.co.uk") == "acme.co.uk"


def test_throttle_prone_backends_have_stricter_rate_caps() -> None:
    # Workable/BambooHR/SmartRecruiters threw a 429 storm under the default rate; their per-domain
    # caps must be present and below the AsyncFetcher default (5/s) so a dense window can't burst.
    from ergon_tracker.http import _DOMAIN_RATE_OVERRIDES

    for dom in ("workable.com", "bamboohr.com", "smartrecruiters.com"):
        assert dom in _DOMAIN_RATE_OVERRIDES, f"{dom} missing a per-domain rate cap"
        rate, period = _DOMAIN_RATE_OVERRIDES[dom]
        assert rate / period < 5.0  # stricter than the constructor default


def test_host_limiter_uses_domain_override() -> None:
    # The limiter for a capped backend must reflect the override, not the constructor default.
    from ergon_tracker.http import _DOMAIN_RATE_OVERRIDES, AsyncFetcher

    f = AsyncFetcher(per_host_rate=5)
    lim = f._host_limiter("workable.com")
    assert lim.max_rate == _DOMAIN_RATE_OVERRIDES["workable.com"][0]
