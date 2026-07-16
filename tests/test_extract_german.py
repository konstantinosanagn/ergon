"""Tests for the German (DE) vocab added to the yoe/degree/comp extractors.

Each test constructs an ``ExtractInput`` with ``language="de"`` directly (the unit under test),
plus one end-to-end check that language detection + ``enrich_in_place`` wire it up automatically,
and one explicit English regression check per extractor to prove the default path is untouched.
"""

from __future__ import annotations

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.comp import CompExtractor, parse_salary
from ergon_tracker.extract.degree import DegreeExtractor
from ergon_tracker.extract.yoe import YoeExtractor
from ergon_tracker.models import JobPosting, SalaryInterval

_YOE = YoeExtractor()
_DEGREE = DegreeExtractor()
_COMP = CompExtractor()


# --- yoe -----------------------------------------------------------------------------------


def test_german_yoe_mindestens_years() -> None:
    inp = ExtractInput(
        title="Rolle",
        description_text="Wir suchen jemanden mit mindestens 5 Jahren Berufserfahrung",
        language="de",
    )
    assert _YOE.extract(inp) == (5, None)


def test_german_yoe_range() -> None:
    inp = ExtractInput(
        title="Rolle",
        description_text="Sie bringen 3-5 Jahre Berufserfahrung mit.",
        language="de",
    )
    assert _YOE.extract(inp) == (3, 5)


def test_german_yoe_vague_band_langjaehrige() -> None:
    inp = ExtractInput(
        title="Rolle",
        description_text="Wir suchen eine Person mit langjährige Erfahrung im Vertrieb.",
        language="de",
    )
    assert _YOE.extract(inp) == (5, None)


def test_english_yoe_regression() -> None:
    inp = ExtractInput(
        title="Role",
        description_text="You have at least 5 years of experience in software engineering.",
    )
    assert _YOE.extract(inp) == (5, None)


# --- degree ----------------------------------------------------------------------------------


def test_german_degree_masterstudium() -> None:
    inp = ExtractInput(
        title="Rolle", description_text="Abgeschlossenes Masterstudium", language="de"
    )
    degree_min, _ = _DEGREE.extract(inp)
    assert degree_min == "master"


def test_german_degree_masterabschluss() -> None:
    inp = ExtractInput(title="Rolle", description_text="Masterabschluss", language="de")
    degree_min, _ = _DEGREE.extract(inp)
    assert degree_min == "master"


def test_german_degree_ausbildung_is_not_bachelor() -> None:
    inp = ExtractInput(title="Rolle", description_text="Abgeschlossene Ausbildung", language="de")
    degree_min, degree_required = _DEGREE.extract(inp)
    assert degree_min != "bachelor"
    assert degree_min in (None, "vocational")


def test_english_degree_regression() -> None:
    inp = ExtractInput(title="Role", description_text="Bachelor's degree in Computer Science")
    degree_min, _ = _DEGREE.extract(inp)
    assert degree_min == "bachelor"


# --- comp ------------------------------------------------------------------------------------


def test_german_salary_brutto_per_year() -> None:
    salary = parse_salary("46.000-59.000 EUR brutto pro Jahr", lang="de")
    assert salary is not None
    assert salary.min_amount == 46000
    assert salary.max_amount == 59000
    assert salary.currency == "EUR"
    assert salary.interval == SalaryInterval.YEAR


def test_german_salary_hours_false_positive_rejected() -> None:
    # "38,5 h/Woche" ("38.5 hours/week") must never be read as a salary figure.
    assert parse_salary("Vollzeit, 38,5 h/Woche", lang="de") is None


def test_german_salary_via_comp_extractor() -> None:
    inp = ExtractInput(
        title="Rolle",
        description_text="Wir bieten ein Jahresgehalt von 46.000-59.000 EUR brutto pro Jahr.",
        language="de",
    )
    salary = _COMP.extract(inp)
    assert salary is not None
    assert salary.min_amount == 46000
    assert salary.max_amount == 59000
    assert salary.currency == "EUR"


def test_english_comp_regression() -> None:
    salary = parse_salary("The salary range for this role is $120,000 - $150,000 per year.")
    assert salary is not None
    assert salary.min_amount == 120000
    assert salary.max_amount == 150000
    assert salary.currency == "USD"
    assert salary.interval == SalaryInterval.YEAR


# --- end-to-end: language detection wired through enrich_in_place ----------------------------


def test_enrich_in_place_detects_german_and_extracts() -> None:
    job = JobPosting.create(
        source="s",
        source_job_id="1",
        company="Acme GmbH",
        title="Softwareentwickler",
        description_text=(
            "Wir suchen ab sofort eine Softwareentwicklerin (m/w/d) für unser Team in Berlin. "
            "Sie bringen mindestens 5 Jahren Berufserfahrung mit und arbeiten gerne im Team. "
            "Wir bieten Ihnen ein Jahresgehalt von 46.000-59.000 EUR brutto pro Jahr sowie "
            "flexible Arbeitszeiten."
        ),
    )
    enrich_in_place(job)
    assert job.years_experience_min == 5
    assert job.years_experience_max is None
    assert job.salary is not None
    assert job.salary.min_amount == 46000
    assert job.salary.max_amount == 59000
    assert job.salary.currency == "EUR"


def test_enrich_in_place_english_regression() -> None:
    job = JobPosting.create(
        source="s",
        source_job_id="2",
        company="Acme",
        title="Software Engineer",
        description_text=(
            "You will need at least 5 years of experience. The salary range for this role "
            "is $120,000 - $150,000 per year."
        ),
    )
    enrich_in_place(job)
    assert job.years_experience_min == 5
    assert job.salary is not None
    assert job.salary.min_amount == 120000
    assert job.salary.max_amount == 150000
