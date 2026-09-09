"""Tests for the table-backed company -> sector classifier."""

from __future__ import annotations

import pytest

from ergon.extract.base import ExtractInput
from ergon.extract.sector import SectorExtractor, load_sector_index


@pytest.fixture(scope="module")
def extractor() -> SectorExtractor:
    return SectorExtractor()


def _sector(extractor: SectorExtractor, key: str) -> str | None:
    return extractor.extract(ExtractInput(title="", company_key=key))


def test_lookup_hits_by_key(extractor: SectorExtractor) -> None:
    # A handful of well-known table entries resolve to their sector.
    assert _sector(extractor, "1password") == "Cybersecurity"
    assert _sector(extractor, "2k") == "Gaming"
    assert _sector(extractor, "10xgenomics") == "Biotech/Pharma"


def test_lookup_is_case_insensitive(extractor: SectorExtractor) -> None:
    assert _sector(extractor, "APEX") == _sector(extractor, "apex")


def test_lookup_by_domain(extractor: SectorExtractor) -> None:
    idx = load_sector_index()
    assert idx.get(domain="cohesity.com") == "Cybersecurity"


def test_unknown_company_returns_none(extractor: SectorExtractor) -> None:
    assert _sector(extractor, "this-company-does-not-exist-xyz") is None


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # Corrected classifications (reasoned from what the company does):
        ("apex", "Aerospace/Defense"),  # satellite / spacecraft manufacturer
        ("toast", "Software/SaaS"),  # restaurant management SaaS platform
        ("brain-co", "AI/ML"),  # applied-AI startup
        ("higharc", "Software/SaaS"),  # homebuilding cloud / SaaS platform
        ("mariana-minerals", "Energy/Climate"),  # critical minerals, energy transition
        ("bellese", "Consulting/Services"),  # govt healthcare service-design consultancy
        ("artera", "AI/ML"),  # AI patient-communication agents
        ("agr", "Insurance"),  # insurance brokerage
        ("align", "Cybersecurity"),  # A-LIGN compliance / security
        ("aerispartners.com", "Banking/Finance"),  # boutique investment bank
        ("appnation", "AI/ML"),  # AI-powered app publisher
        ("solva", "Fintech"),  # digital non-bank lender
    ],
)
def test_corrected_company_sectors(extractor: SectorExtractor, key: str, expected: str) -> None:
    assert _sector(extractor, key) == expected


# --- company-name fallback (applied only when the curated table misses) ---

from ergon.extract.sector import name_sector  # noqa: E402


@pytest.mark.parametrize(
    "company,expected",
    [
        ("C6 Bank", "Banking/Finance"),
        ("AYANA Hospitality", "Travel/Hospitality"),
        ("Challenge Manufacturing", "Manufacturing/Industrial"),
        ("BridgeBio Pharma", "Biotech/Pharma"),
        ("Clarkson Eyecare", "Healthcare"),
        ("Centria Autism", "Healthcare"),
        ("Arco Educação", "Education"),  # accent-folded
        ("American University of Bahrain", "Education"),
        ("Centennial Real Estate Company LLC", "RealEstate/PropTech"),
        ("Acme Reinsurance Group", "Insurance"),
    ],
)
def test_name_sector_high_precision_hits(company: str, expected: str) -> None:
    assert name_sector(company) == expected


@pytest.mark.parametrize(
    "company",
    ["Boxlunch", "Whoop", "Canonical", "Anduril Industries", "Bolt Technology", "", None],
)
def test_name_sector_opaque_names_stay_unknown(company: str | None) -> None:
    # Generic/opaque names must NOT be guessed (precision over recall). "Industries",
    # "Technology" are deliberately excluded as low-signal.
    assert name_sector(company) is None


def test_extractor_prefers_table_over_name(extractor: SectorExtractor) -> None:
    # When the curated table has the company, that authoritative value wins even if the
    # name also contains an industry word.
    table_sector = extractor.extract(ExtractInput(title="", company_key="1password"))
    assert table_sector == "Cybersecurity"  # not overridden by any name rule


def test_extractor_falls_back_to_name(extractor: SectorExtractor) -> None:
    # Company absent from the table but with an unambiguous name word -> classified.
    inp = ExtractInput(
        title="Teller", company="Riverside Community Bank", company_key="zzz-not-in-table"
    )
    assert extractor.extract(inp) == "Banking/Finance"


@pytest.mark.parametrize(
    "company,sector",
    [
        ("Domino's", "Food/Beverage"),
        ("Red Bull", "Food/Beverage"),
        ("Anduril Industries", "Aerospace/Defense"),
        ("Veolia Environnement SA", "Energy/Climate"),
        ("Eurofins", "Biotech/Pharma"),
        ("Home Instead", "Healthcare"),
        ("Maersk", "Logistics/SupplyChain"),
        ("Deloitte", "Consulting/Services"),
        ("METRO/MAKRO", "E-commerce/Retail"),
        ("KIPP", "Education"),
        ("Mattel", "Consumer/Lifestyle"),
        ("Canonical", "Software/SaaS"),
        ("Ubisoft", "Gaming"),
        ("Sutherland", "Consulting/Services"),
        ("Shieldai", "Aerospace/Defense"),
        ("altamed", "Healthcare"),
        ("Starbucks", "Food/Beverage"),
        ("qualcomm", "Semiconductors/Hardware"),
        ("SpaceX", "Aerospace/Defense"),
        ("tjx", "E-commerce/Retail"),
        ("jpmc", "Banking/Finance"),
        ("Mount Sinai", "Healthcare"),
        ("verizon", "Telecom"),
        ("medtronic", "Healthcare"),
        ("homedepot", "E-commerce/Retail"),
        ("City of New York", "Government/Public"),
        ("infineon", "Semiconductors/Hardware"),
    ],
)
def test_company_sector_exact_brand_map(company: str, sector: str) -> None:
    # Large opaque-brand employers with no industry word in their name are classified by the
    # high-precision exact company-name map (job-count-weighted coverage win).
    from ergon.extract.sector import company_sector

    assert company_sector(company) == sector


def test_company_sector_unknown_stays_none() -> None:
    from ergon.extract.sector import company_sector

    assert company_sector("Some Opaque Holdings LLC") is None
    assert company_sector(None) is None


def test_extractor_uses_company_map_when_registry_key_misses(
    extractor: SectorExtractor,
) -> None:
    # The registry key isn't in the curated table (as happens for franchise/aggregator postings),
    # but the exact company-name map still classifies the brand.
    inp = ExtractInput(title="Assistant Manager", company="Domino's", company_key="not-in-table")
    assert extractor.extract(inp) == "Food/Beverage"
