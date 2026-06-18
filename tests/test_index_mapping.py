from datetime import datetime, timezone

from ergon_tracker.index.mapping import from_row, to_row
from ergon_tracker.models import (
    JobLevel,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SalaryInterval,
)


def _job():
    return JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Stripe",
        title="Senior Backend Engineer",
        company_domain="stripe.com",
        description_text="Build payments. Rust and Go.",
        locations=[Location(city="Berlin", country="Germany", raw="Berlin, Germany")],
        remote=RemoteType.REMOTE,
        level=JobLevel.SENIOR,
        sector="Fintech",
        salary=Salary(min_amount=120000, max_amount=160000, currency="USD",
                      interval=SalaryInterval.YEAR),
        apply_url="https://x/1",
        posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        visa_sponsor=True,
        visa_last_filed="2026-03-31",
        sponsorship_offered=True,
    )


def test_round_trip_preserves_indexed_fields():
    j = _job()
    j2 = from_row(to_row(j, build_id="b1"))
    assert j2.id == j.id and j2.company == "Stripe" and j2.title == j.title
    assert j2.level is JobLevel.SENIOR and j2.remote is RemoteType.REMOTE
    assert j2.sector == "Fintech" and j2.visa_sponsor is True and j2.sponsorship_offered is True
    assert j2.salary.min_amount == 120000 and j2.salary.currency == "USD"
    assert j2.locations[0].city == "Berlin" and j2.locations[0].country == "Germany"


def test_to_row_sets_role_family_and_company_key():
    from ergon_tracker.dedup import normalize_company, normalize_title

    row = to_row(_job(), build_id="b1")
    assert row["company_key"] == normalize_company("Stripe")
    assert row["role_family"] == normalize_title("Senior Backend Engineer")
    assert row["snippet"].startswith("Build payments")
