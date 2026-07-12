"""Regression test for the explicit-country ISO alpha-2 canonicalization fallback.

Stage-1 final-review follow-up: ``normalize_geo`` canonicalizes an already-set
``Location.country`` via ``_COUNTRY_ALIASES`` only. That table covers "us"/"uk"/"uae"
(codes that double as common aliases) but not the rest of the ISO alpha-2 codes
("GB", "DE", ...), which live in ``_ISO2_COUNTRY``. So a lever that sets
``Location.country`` from a raw ISO code left non-US countries un-expanded
("GB" stayed "GB" instead of becoming "United Kingdom"), weakening country filtering.
"""

from ergon_tracker.extract.geo import normalize_geo
from ergon_tracker.models import Location


def test_explicit_country_gb_expands_to_united_kingdom() -> None:
    loc = Location(country="GB")
    normalize_geo(loc)
    assert loc.country == "United Kingdom"


def test_explicit_country_de_expands_to_germany() -> None:
    loc = Location(country="DE")
    normalize_geo(loc)
    assert loc.country == "Germany"


def test_explicit_country_iso2_is_case_insensitive() -> None:
    loc = Location(country="gb")
    normalize_geo(loc)
    assert loc.country == "United Kingdom"


def test_explicit_country_us_still_resolves() -> None:
    loc = Location(country="US")
    normalize_geo(loc)
    assert loc.country == "United States"

    loc2 = Location(country="usa")
    normalize_geo(loc2)
    assert loc2.country == "United States"

    loc3 = Location(country="United States")
    normalize_geo(loc3)
    assert loc3.country == "United States"


def test_explicit_country_full_name_unchanged() -> None:
    loc = Location(country="Canada")
    normalize_geo(loc)
    assert loc.country == "Canada"


def test_explicit_country_unknown_two_letter_does_not_crash() -> None:
    # "ZZ" is not a real ISO alpha-2 country code and not in _COUNTRY_ALIASES; it should be
    # left as-is rather than mis-expanded or raising.
    loc = Location(country="ZZ")
    normalize_geo(loc)
    assert loc.country == "ZZ"
