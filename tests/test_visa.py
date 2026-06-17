"""H-1B visa-sponsor: ETL parsing, enrichment, and filter."""

from __future__ import annotations

import sys
from pathlib import Path

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.extract import visa
from ergon_tracker.extract.visa import SponsorIndex
from ergon_tracker.models import JobPosting, SearchQuery

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_h1b_sponsors import sponsors_from_rows  # noqa: E402


def _job(company: str) -> JobPosting:
    return JobPosting.create(source="greenhouse", source_job_id="1", company=company, title="Eng")


def _idx(**recs: dict[str, object]) -> SponsorIndex:
    return SponsorIndex(dict(recs))


# --- ETL parsing (pure, no workbook needed) ---------------------------------
def test_sponsors_from_rows_keeps_certified_normalizes_and_dates() -> None:
    rows = [
        {"EMPLOYER_NAME": "Stripe, Inc.", "CASE_STATUS": "Certified", "DECISION_DATE": "2025-01-10"},
        {"EMPLOYER_NAME": "STRIPE INC", "CASE_STATUS": "Certified - Withdrawn",
         "DECISION_DATE": "2025-06-30"},  # same employer, later date
        {"EMPLOYER_NAME": "Denied Co", "CASE_STATUS": "Denied", "DECISION_DATE": "2025-05-01"},
        {"EMPLOYER_NAME": "Acme GmbH", "CASE_STATUS": "CERTIFIED", "DECISION_DATE": "2024-11-02"},
        {"EMPLOYER_NAME": "", "CASE_STATUS": "Certified", "DECISION_DATE": "2025-01-01"},
    ]
    out = sponsors_from_rows(rows)
    assert out["stripe"]["n"] == 2
    assert out["stripe"]["last"] == "2025-06-30"  # most-recent filing kept
    assert out["acme"]["n"] == 1
    assert "denied co" not in out


def test_sponsors_from_rows_handles_alt_header_and_us_dates() -> None:
    rows = [{"EMPLOYER_LEGAL_BUSINESS_NAME": "Globex LLC", "case_status": "Certified",
             "RECEIVED_DATE": "03/15/2025"}]
    out = sponsors_from_rows(rows)
    assert out["globex"]["n"] == 1
    assert out["globex"]["last"] == "2025-03-15"  # M/D/Y parsed to ISO


# --- enrichment -------------------------------------------------------------
def test_enrich_sets_visa_sponsor_and_last_filed(monkeypatch) -> None:
    idx = _idx(stripe={"n": 5, "last": "2025-06-30"})
    monkeypatch.setattr(visa, "load_sponsor_index", lambda: idx)
    monkeypatch.setattr("ergon_tracker.enrich.is_h1b_sponsor", lambda c: idx.is_sponsor(c))
    monkeypatch.setattr("ergon_tracker.enrich.h1b_last_filed", lambda c: idx.last_filed(c))

    job = _job("Stripe, Inc.")
    enrich_in_place(job)
    assert job.visa_sponsor is True
    assert job.visa_last_filed == "2025-06-30"

    other = _job("Nobody Co")
    enrich_in_place(other)
    assert other.visa_sponsor is None  # positive evidence only — never False
    assert other.visa_last_filed is None


def test_index_missing_file_is_graceful() -> None:
    idx = SponsorIndex({})
    assert idx.is_sponsor("Stripe") is False
    assert idx.last_filed("Stripe") is None
    assert len(idx) == 0


# --- filter -----------------------------------------------------------------
def test_visa_sponsor_filter() -> None:
    sponsor = _job("Stripe")
    sponsor.visa_sponsor = True
    unknown = _job("Nobody Co")

    q = SearchQuery(visa_sponsor=True)
    assert q.matches(sponsor) is True
    assert q.matches(unknown) is False
    assert SearchQuery().matches(unknown) is True  # no filter -> kept


def test_sponsor_index_normalizes_lookup() -> None:
    idx = _idx(stripe={"n": 1, "last": "2025-01-01"})
    assert idx.is_sponsor("Stripe, Inc.") is True
    assert idx.is_sponsor("STRIPE") is True
    assert idx.last_filed("Stripe, Inc.") == "2025-01-01"
    assert idx.is_sponsor("Unrelated") is False
    assert idx.is_sponsor(None) is False


# --- gated leading-token matching (the "creative" fallback) -----------------
def test_gated_leading_token_accepts_corporate_descriptor() -> None:
    # Posting "Spotify" should match the LCA legal name "Spotify USA" (geo descriptor).
    idx = _idx(**{"spotify usa": {"n": 3, "last": "2026-03-01"}})
    assert idx.is_sponsor("Spotify") is True
    assert idx.last_filed("Spotify") == "2026-03-01"

    idx2 = _idx(**{"palantir technologies": {"n": 1, "last": "2025-09-09"}})
    assert idx2.is_sponsor("Palantir") is True


def test_gated_leading_token_rejects_unrelated_company() -> None:
    # "Linear" (software) must NOT match "Linear Signs" — "signs" isn't a corporate descriptor.
    idx = _idx(**{"linear signs": {"n": 1, "last": "2025-01-01"}})
    assert idx.is_sponsor("Linear") is False
    assert idx.last_filed("Linear") is None

    idx2 = _idx(**{"alan b lancz and associates": {"n": 1, "last": "2025-01-01"}})
    assert idx2.is_sponsor("Alan") is False


def test_exact_still_wins_over_leading_token() -> None:
    idx = _idx(
        stripe={"n": 9, "last": "2026-06-01"},
        **{"stripe payments solutions": {"n": 1, "last": "2020-01-01"}},
    )
    assert idx.last_filed("Stripe") == "2026-06-01"  # exact record, not the leading-token one


# --- on-disk format tolerance ----------------------------------------------
def test_coerce_legacy_formats() -> None:
    from ergon_tracker.extract.visa import _coerce_records

    assert _coerce_records({"a": {"n": 2, "last": "2025-01-01"}})["a"]["last"] == "2025-01-01"
    assert _coerce_records({"a": 3})["a"]["n"] == 3  # legacy count-only
    assert "a" in _coerce_records(["a", "b"])  # legacy list
