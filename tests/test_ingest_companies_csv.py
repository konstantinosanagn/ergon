"""Tests for the curated-CSV registry ingester's pure parsing/slugging logic.

These cover the no-network functions: company-key slugging, CSV row parsing (header/blank/
comment skipping, ats defaulting from the file stem, workday token split, unsupported-ats and
malformed-row rejection, within-file dedupe), and cross-file merge/dedupe. No network, no
seed.json reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ingest_companies_csv import (  # noqa: E402
    company_key,
    merge_candidates,
    parse_csv_rows,
)


def test_company_key_slugifies() -> None:
    assert company_key("Acme, Inc.") == "acme-inc"
    assert company_key("  Spaced  Out ") == "spaced-out"
    assert company_key("1Password") == "1password"
    assert company_key("Foo & Bar / Baz") == "foo-bar-baz"


def test_parse_simple_rows_with_default_ats_from_stem() -> None:
    csv_text = "name,ats,token,domain\nStripe,,stripe,stripe.com\nLinear,,linear,linear.app\n"
    cands, errors = parse_csv_rows(csv_text, default_ats="greenhouse")
    assert errors == []
    assert cands == [
        {"company": "stripe", "ats": "greenhouse", "token": "stripe", "domain": "stripe.com"},
        {"company": "linear", "ats": "greenhouse", "token": "linear", "domain": "linear.app"},
    ]


def test_parse_empty_domain_becomes_null() -> None:
    cands, errors = parse_csv_rows("name,ats,token,domain\nAcme,lever,acme,\n", "lever")
    assert errors == []
    assert cands[0]["domain"] is None


def test_parse_skips_blank_and_comment_lines() -> None:
    csv_text = (
        "name,ats,token,domain\n"
        "# this is a comment\n"
        "\n"
        "Stripe,greenhouse,stripe,stripe.com\n"
        "   \n"
        "# trailing comment\n"
    )
    cands, errors = parse_csv_rows(csv_text, "greenhouse")
    assert errors == []
    assert [c["company"] for c in cands] == ["stripe"]


def test_parse_explicit_ats_overrides_default() -> None:
    cands, _ = parse_csv_rows("name,ats,token,domain\nAcme,ashby,acme,\n", "greenhouse")
    assert cands[0]["ats"] == "ashby"


def test_parse_workday_token_split() -> None:
    csv_text = (
        "name,ats,token,domain\n"
        "NVIDIA,workday,nvidia|wd5|NVIDIAExternalCareerSite,nvidia.com\n"
    )
    cands, errors = parse_csv_rows(csv_text, "workday")
    assert errors == []
    assert cands == [
        {
            "company": "nvidia",
            "ats": "workday",
            "tenant": "nvidia",
            "wd": "wd5",
            "site": "NVIDIAExternalCareerSite",
            "domain": "nvidia.com",
        }
    ]


def test_parse_workday_malformed_token_reported() -> None:
    cands, errors = parse_csv_rows(
        "name,ats,token,domain\nBad,workday,onlytenant,\n", "workday"
    )
    assert cands == []
    assert len(errors) == 1
    assert "tenant|wd|site" in errors[0]


def test_parse_workday_default_ats_from_stem() -> None:
    # No ats cell; stem provides workday and the token still splits.
    cands, errors = parse_csv_rows(
        "name,ats,token,domain\nNVIDIA,,nvidia|wd5|Site,\n", "workday"
    )
    assert errors == []
    assert cands[0]["wd"] == "wd5" and cands[0]["site"] == "Site"


def test_parse_unsupported_ats_rejected() -> None:
    cands, errors = parse_csv_rows(
        "name,ats,token,domain\nAcme,bamboohr,acme,\n", "greenhouse"
    )
    assert cands == []
    assert len(errors) == 1
    assert "unsupported ats" in errors[0]


def test_parse_missing_name_and_token_reported() -> None:
    csv_text = "name,ats,token,domain\n,greenhouse,acme,\nAcme,greenhouse,,\n"
    cands, errors = parse_csv_rows(csv_text, "greenhouse")
    assert cands == []
    assert len(errors) == 2
    assert any("missing name" in e for e in errors)
    assert any("missing token" in e for e in errors)


def test_parse_dedupes_within_file_first_wins() -> None:
    csv_text = (
        "name,ats,token,domain\n"
        "Acme,greenhouse,acme,acme.com\n"
        "ACME,greenhouse,acme2,acme.io\n"  # same slug -> dropped
    )
    cands, _ = parse_csv_rows(csv_text, "greenhouse")
    assert len(cands) == 1
    assert cands[0]["token"] == "acme"


def test_parse_no_header_still_parses() -> None:
    # A file without a header row (first cell != "name") parses every row as data.
    cands, errors = parse_csv_rows("Stripe,greenhouse,stripe,stripe.com\n", "greenhouse")
    assert errors == []
    assert cands[0]["company"] == "stripe"


def test_merge_candidates_dedupes_across_files_first_file_wins() -> None:
    file_a = [{"company": "stripe", "ats": "greenhouse", "token": "stripe", "domain": None}]
    file_b = [
        {"company": "stripe", "ats": "lever", "token": "stripe2", "domain": None},  # dup key
        {"company": "linear", "ats": "ashby", "token": "linear", "domain": None},
    ]
    merged = merge_candidates([file_a, file_b])
    assert [c["company"] for c in merged] == ["stripe", "linear"]
    # first-file entry wins on conflict
    assert merged[0]["ats"] == "greenhouse"
