"""Tests for deterministic geo (city/country) extraction in ``ergon_tracker.extract.geo``."""

from __future__ import annotations

import pytest

from ergon_tracker.extract.geo import normalize_geo
from ergon_tracker.models import Location


def _geo(raw: str) -> Location:
    return normalize_geo(Location(raw=raw))


@pytest.mark.parametrize(
    ("raw", "city", "country"),
    [
        # City that is also a state name + ATS noise -> prefer the gazetteer city.
        ("New York, NY (HQ)", "New York", "United States"),
        ("New York", "New York", "United States"),
        ("New York, NY", "New York", "United States"),
        # City that is also a country alias.
        ("Singapore", "Singapore", "Singapore"),
        # Plain city,country and city,state pairs.
        ("Berlin, Germany", "Berlin", "Germany"),
        ("Austin, TX", "Austin", "United States"),
    ],
)
def test_city_and_country(raw: str, city: str, country: str) -> None:
    loc = _geo(raw)
    assert loc.city == city
    assert loc.country == country


def test_sublocation_uses_leading_city() -> None:
    # The leading word(s) form a gazetteer city; the facility suffix is dropped.
    assert _geo("Boston Drydock").city == "Boston"
    assert _geo("Boston Drydock").country == "United States"
    assert _geo("Barcelona Gran Vía").city == "Barcelona"
    assert _geo("Singapore - Woodlands - NorthTech").city == "Singapore"


def test_accent_folding() -> None:
    loc = _geo("İstanbul")
    assert loc.city == "Istanbul"
    assert loc.country == "Turkey"


def test_non_city_fragments_rejected() -> None:
    # No gazetteer city present and the segment looks like a sub-location -> no bogus city.
    assert _geo("Texas - Depot 2").city is None
    assert _geo("California - LA Depot").city is None


def test_no_place_yields_no_city() -> None:
    assert _geo("3 Locations").city is None
    assert _geo("Remote").city is None


def test_parenthetical_country_qualifier_wins_over_gazetteer() -> None:
    # A parenthetical country/state qualifier disambiguates an otherwise-ambiguous city; without
    # this it was stripped as noise and the city fell to the wrong gazetteer default (London -> UK,
    # Hamilton -> Canada) — the "location shows the wrong country" class a user reported.
    assert _geo("London (Canada)").country == "Canada"
    assert _geo("London (Canada)").city == "London"
    assert _geo("Hamilton (New Zealand)").country == "New Zealand"
    assert _geo("Toronto (Canada)").country == "Canada"
    assert _geo("Springfield (Illinois)").country == "United States"


def test_parenthetical_country_sets_remote_country() -> None:
    # "Remote (US)" previously lost its only country signal to the parenthetical-noise strip.
    for raw in ("Remote (US)", "Remote (USA)", "Remote (United States)"):
        loc = _geo(raw)
        assert loc.is_remote is True
        assert loc.country == "United States"


def test_parenthetical_bare_state_code_not_forced_us() -> None:
    # A bare 2-letter code in parens is ambiguous (CA = California or Canada) — must NOT force US.
    loc = _geo("Vancouver (CA)")
    assert loc.country != "United States"  # stays Canada via the gazetteer, not mislabeled US


def test_anywhere_is_not_a_city() -> None:
    assert _geo("Remote - Anywhere").city is None
    assert _geo("Remote - Anywhere").is_remote is True


def test_does_not_overwrite_preset_fields() -> None:
    loc = Location(raw="Berlin, Germany", city="Munich", country="Austria")
    normalize_geo(loc)
    assert loc.city == "Munich"
    assert loc.country == "Austria"


def test_canonicalizes_preset_country_code() -> None:
    # Providers emit structured country codes ("US"/"USA"); canonicalize them so the index
    # does not fragment "US" (9.6k rows) vs "United States" (48k rows) for country filters.
    for code in ("US", "us", "USA", "U.S.A.", "united states of america"):
        loc = Location(raw="New York", country=code)
        normalize_geo(loc)
        assert loc.country == "United States", code


def test_canonical_country_left_unchanged() -> None:
    loc = Location(raw="London", country="United Kingdom")
    normalize_geo(loc)
    assert loc.country == "United Kingdom"


def test_canonicalizes_country_with_empty_raw() -> None:
    loc = Location(raw="", country="US")  # structured-only location, no raw string
    normalize_geo(loc)
    assert loc.country == "United States"
