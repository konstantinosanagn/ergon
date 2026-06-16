"""Unit tests for the seed registry store (offline, packaged-data loader)."""

from __future__ import annotations

from jobspine.registry.store import SeedRegistry


def test_seed_loads_and_is_nonempty() -> None:
    reg = SeedRegistry()
    assert len(reg) > 0
    assert len(reg.all()) == len(reg)


def test_get_returns_entry_for_known_key() -> None:
    reg = SeedRegistry()
    entry = reg.get("stripe")
    assert entry is not None
    assert entry["ats"] == "greenhouse"
    assert entry["token"] == "stripe"
    assert entry["domain"] == "stripe.com"


def test_get_unknown_key_returns_none() -> None:
    assert SeedRegistry().get("definitely-not-a-company") is None


def test_lookup_domain_resolves_known_company() -> None:
    res = SeedRegistry().lookup_domain("stripe.com")
    assert res is not None
    assert res.matched is True
    assert bool(res) is True
    assert res.ats == "greenhouse"
    assert res.token == "stripe"
    assert res.domain == "stripe.com"


def test_lookup_domain_is_forgiving_about_www_and_subdomains() -> None:
    reg = SeedRegistry()
    assert reg.lookup_domain("www.spotify.com") is not None
    careers = reg.lookup_domain("careers.spotify.com")
    assert careers is not None
    assert careers.ats == "lever"
    assert careers.token == "spotify"


def test_lookup_domain_workday_composite_token() -> None:
    res = SeedRegistry().lookup_domain("nvidia.com")
    assert res is not None
    assert res.ats == "workday"
    assert res.token == "nvidia|wd5|NVIDIAExternalCareerSite"


def test_lookup_domain_unknown_returns_none() -> None:
    assert SeedRegistry().lookup_domain("unknown.example") is None


def test_all_entries_have_required_fields() -> None:
    reg = SeedRegistry()
    for key, entry in reg.all().items():
        assert entry.get("ats"), key
        assert entry.get("token"), key
        assert entry.get("domain"), key
