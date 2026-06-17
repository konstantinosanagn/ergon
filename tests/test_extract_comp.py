"""Offline tests for the compensation extractor (no network)."""

from __future__ import annotations

import pytest

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.comp import CompExtractor, parse_salary
from ergon_tracker.models import Salary, SalaryInterval

EX = CompExtractor()


def _run(text: str) -> Salary | None:
    return EX.extract(ExtractInput(title="Engineer", description_text=text))


# --- structured passthrough ---------------------------------------------------


def test_structured_salary_passthrough() -> None:
    s = Salary(min_amount=120_000, max_amount=160_000, currency="USD", interval=SalaryInterval.YEAR)
    inp = ExtractInput(
        title="Engineer",
        description_text="Pay range: $999,999 - $1,000,000 per year",  # should be ignored
        structured_salary=s,
    )
    assert EX.extract(inp) is s  # same object, untouched


def test_structured_salary_empty_falls_through_to_parse() -> None:
    empty = Salary(currency="USD")  # no amounts -> not authoritative
    inp = ExtractInput(
        title="Engineer",
        description_text="Salary range: $120,000 - $160,000 per year",
        structured_salary=empty,
    )
    out = EX.extract(inp)
    assert out is not None and out.min_amount == 120_000 and out.max_amount == 160_000


def test_falls_back_to_title() -> None:
    out = EX.extract(
        ExtractInput(title="Senior Engineer ($150k-$180k)", description_text="No pay here.")
    )
    assert out is not None
    assert out.min_amount == 150_000 and out.max_amount == 180_000


# --- ranges -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,lo,hi",
    [
        ("Salary range: $120,000 - $160,000 per year", 120_000, 160_000),
        ("Compensation: $120,000–$160,000 annually", 120_000, 160_000),  # en dash
        ("Base salary $120k-$160k", 120_000, 160_000),
        ("The pay band is 120K to 160K", 120_000, 160_000),
        ("Salary between $90,000 and $110,000", 90_000, 110_000),
        ("Comp: $99,500 — $145,750 per annum", 99_500, 145_750),  # em dash
    ],
)
def test_ranges(text: str, lo: float, hi: float) -> None:
    out = _run(text)
    assert out is not None
    assert out.min_amount == lo
    assert out.max_amount == hi


# --- single values & open bounds ----------------------------------------------


def test_single_value() -> None:
    out = _run("Salary: $150,000 per year")
    assert out is not None
    assert out.min_amount == 150_000 and out.max_amount == 150_000


def test_single_k_with_cue() -> None:
    out = _run("We offer a competitive salary of 150k.")
    assert out is not None
    assert out.min_amount == 150_000


def test_from_lower_bound_only() -> None:
    out = _run("Compensation starts from $90,000 depending on experience.")
    assert out is not None
    assert out.min_amount == 90_000
    assert out.max_amount is None


def test_up_to_upper_bound_only() -> None:
    out = _run("Salary up to $200k for the right candidate.")
    assert out is not None
    assert out.min_amount is None
    assert out.max_amount == 200_000


# --- currencies ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,ccy,lo",
    [
        ("Salary: $120,000 per year", "USD", 120_000),
        ("Salary: £80,000 per year", "GBP", 80_000),
        ("Salary: €80.000,00 per year", "EUR", 80_000),  # EU formatting
        ("Salary: C$130,000 annually", "CAD", 130_000),
        ("Salary: CA$130,000 annually", "CAD", 130_000),
        ("Salary: A$140,000 annually", "AUD", 140_000),
        ("Salary: US$120,000 annually", "USD", 120_000),
        ("Salary: 120,000 USD per year", "USD", 120_000),
    ],
)
def test_currencies(text: str, ccy: str, lo: float) -> None:
    out = _run(text)
    assert out is not None
    assert out.currency == ccy
    assert out.min_amount == lo


def test_eu_vs_us_decimal_disambiguation() -> None:
    us = _run("Salary: $80,000.50 per year")
    eu = _run("Salary: €80.000,50 per year")
    assert us is not None and us.min_amount == 80_000.50
    assert eu is not None and eu.min_amount == 80_000.50


# --- intervals ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,interval",
    [
        ("Salary: $120,000 per year", SalaryInterval.YEAR),
        ("Salary: $120,000 /year", SalaryInterval.YEAR),
        ("Salary: $120,000 per annum", SalaryInterval.YEAR),
        ("Salary: $120,000 annually", SalaryInterval.YEAR),
        ("Hourly rate $45/hr", SalaryInterval.HOUR),
        ("Pay: $45 per hour", SalaryInterval.HOUR),
        ("Stipend of $6,000 per month", SalaryInterval.MONTH),
        ("Contract pay $2,500 per week", SalaryInterval.WEEK),
        ("Day rate of $800 per day", SalaryInterval.DAY),
    ],
)
def test_intervals(text: str, interval: SalaryInterval) -> None:
    out = _run(text)
    assert out is not None
    assert out.interval == interval


def test_infer_year_for_large_numbers() -> None:
    out = _run("Salary range $120,000 - $160,000")
    assert out is not None
    assert out.interval == SalaryInterval.YEAR


def test_infer_hour_for_small_currency_amount() -> None:
    out = _run("Compensation: $45 hourly")
    assert out is not None
    assert out.interval == SalaryInterval.HOUR
    assert out.min_amount == 45


def test_ote_cue() -> None:
    out = _run("OTE: $180,000 - $220,000")
    assert out is not None
    assert out.min_amount == 180_000 and out.max_amount == 220_000


# --- false-positive guards ----------------------------------------------------


def test_401k_not_salary() -> None:
    assert _run("Great 401k match and 5+ years experience required.") is None


def test_401k_parens_not_salary() -> None:
    assert _run("We offer a 401(k) plan with company match.") is None


def test_years_experience_not_salary() -> None:
    assert _run("Looking for someone with 5+ years of experience and a degree.") is None


def test_zip_code_not_salary() -> None:
    assert _run("Our office is at 1600 Amphitheatre Pkwy, Mountain View 94043.") is None


def test_phone_number_not_salary() -> None:
    assert _run("Questions? Call us at 555-123-4567 anytime.") is None


def test_24_7_not_salary() -> None:
    assert _run("This is a 24/7 on-call rotation role.") is None


def test_equity_percentage_not_salary() -> None:
    assert _run("Includes 0.5% equity and a 10% annual bonus target.") is None


def test_zero_dollars_not_salary() -> None:
    assert _run("Application fee: $0. Apply today!") is None


def test_k_without_cue_not_salary() -> None:
    # "10k" here is a metric, not pay — no currency, no salary cue.
    assert _run("Join our community of 10k engineers building great things.") is None


def test_plain_text_returns_none() -> None:
    assert _run("We are a fast-growing startup hiring engineers.") is None


def test_empty_inputs() -> None:
    assert parse_salary(None) is None
    assert parse_salary("") is None
    assert EX.extract(ExtractInput(title="Engineer")) is None


def test_picks_salary_over_bonus() -> None:
    out = _run("$5,000 signing bonus plus base salary of $120k-$160k per year.")
    assert out is not None
    assert out.min_amount == 120_000 and out.max_amount == 160_000


def test_currency_inherited_across_range() -> None:
    out = _run("Salary: $120k - 160k")
    assert out is not None
    assert out.currency == "USD"
    assert out.min_amount == 120_000 and out.max_amount == 160_000


# --- financial / scale distractors are not compensation -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "We raised $50M in Series B funding last year.",
        "The company hit a $2B valuation in 2025.",
        "Now serving 10,000 customers across the globe.",
        "We crossed $5M ARR this quarter.",
        "We've raised $781M in funding from top investors.",
        "Backed by €280M in total funding from leading VCs.",
        "Solva is transforming an $8 trillion industry.",
        "Accelerating growth within the $10K-$100K ARR merchant segment.",
        "Over $200B in annualized spend flows through our platform.",
        "Join our community of 50,000 users and 2,000 companies.",
    ],
)
def test_financial_and_scale_numbers_not_salary(text: str) -> None:
    assert _run(text) is None


def test_salary_survives_financial_distractor() -> None:
    out = _run("We raised $50M. Salary range $120,000-$160,000/year.")
    assert out is not None
    assert out.min_amount == 120_000 and out.max_amount == 160_000


def test_million_salary_rejected_as_scale() -> None:
    # Seven-figure "salary" is implausible per period — treat as a scale figure.
    assert _run("Salary budget of $5,000,000 per year for the whole team.") is None
