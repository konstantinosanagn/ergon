"""Salary.as_text() must never crash on partial data (the 'max=None blew up the format' bug)."""

from __future__ import annotations

import pytest

from ergon_tracker.models import Salary, SalaryInterval


@pytest.mark.parametrize(
    "salary,expected",
    [
        (Salary(), ""),  # nothing
        (Salary(min_amount=120000), "120,000"),  # min only — the crash case
        (Salary(max_amount=150000), "150,000"),  # max only
        (Salary(min_amount=120000, max_amount=150000), "120,000–150,000"),  # range
        (Salary(min_amount=150000, max_amount=150000), "150,000"),  # equal -> single
        (Salary(min_amount=120000, currency="USD"), "USD 120,000"),
        (
            Salary(
                min_amount=120000, max_amount=150000, currency="USD", interval=SalaryInterval.YEAR
            ),
            "USD 120,000–150,000/year",
        ),
        (Salary(max_amount=60, currency="USD", interval=SalaryInterval.HOUR), "USD 60/hour"),
    ],
)
def test_as_text(salary: Salary, expected: str) -> None:
    assert salary.as_text() == expected


def test_as_text_never_raises_on_any_partial_combo() -> None:
    # Exhaustively exercise the None-combinations that broke the ad-hoc crawl.
    for lo in (None, 0, 100000.0):
        for hi in (None, 0, 150000.0):
            for cur in (None, "USD"):
                for interval in (None, SalaryInterval.YEAR):
                    Salary(
                        min_amount=lo, max_amount=hi, currency=cur, interval=interval
                    ).as_text()  # must not raise
