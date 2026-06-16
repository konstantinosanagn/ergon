"""Tests for the keyless token-variation harvester's pure (no-network) logic.

These cover slug generation (ordering, dedupe, corporate-suffix stripping, ``the`` removal,
domain-derived variants), the registry company-key slugger, and input-file parsing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_tokens import (  # noqa: E402
    company_key,
    generate_token_variations,
    parse_companies,
)


def test_company_key_slugs_and_strips_leading_the() -> None:
    assert company_key("Acme Labs, Inc.") == "acme-labs-inc"
    assert company_key("The Foo Company") == "foo-company"
    assert company_key("  Spaced   Out  ") == "spaced-out"


def test_generate_token_variations_is_ordered_and_deduped() -> None:
    variants = generate_token_variations("Acme Labs")
    # most-likely first: lowercase no-spaces leads
    assert variants[0] == "acmelabs"
    assert "acme-labs" in variants
    assert "AcmeLabs" in variants
    # no duplicates, all non-empty
    assert len(variants) == len(set(variants))
    assert all(v for v in variants)


def test_generate_token_variations_strips_corporate_suffixes() -> None:
    variants = generate_token_variations("Globex Corp")
    # suffix-stripped core ("Corp" removed) is offered as its own candidate
    assert "globex" in variants
    # the full form is still present (suffix-stripping is additive, not destructive)
    assert "globexcorp" in variants
    # multiple trailing suffixes peel off (Inc + GmbH)
    multi = generate_token_variations("Initech Inc GmbH")
    assert "initech" in multi


def test_generate_token_variations_handles_leading_the() -> None:
    variants = generate_token_variations("The Foo Company")
    assert "thefoocompany" in variants  # literal form kept
    assert "foocompany" in variants  # "the"-removed form added


def test_generate_token_variations_uses_domain_second_level_label() -> None:
    variants = generate_token_variations("Some Brand", domain="cool-brand.co.uk")
    assert "coolbrand" in variants
    # full URL form is tolerated too
    v2 = generate_token_variations("X", domain="https://jobs.acme.com/careers")
    assert "acme" in v2


def test_generate_token_variations_count_is_bounded() -> None:
    variants = generate_token_variations("Big Long Company Holdings GmbH")
    assert 1 <= len(variants) <= 20


def test_parse_companies_ignores_blanks_and_comments_and_reads_domains() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "Stripe, stripe.com",
            "Ramp",
            "   ",
            "Acme Labs,acme.com",
        ]
    )
    parsed = parse_companies(text)
    assert parsed == [
        ("Stripe", "stripe.com"),
        ("Ramp", None),
        ("Acme Labs", "acme.com"),
    ]
