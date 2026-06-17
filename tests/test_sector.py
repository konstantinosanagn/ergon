"""Tests for the table-backed company -> sector classifier."""

from __future__ import annotations

import pytest

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.sector import SectorExtractor, load_sector_index


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
