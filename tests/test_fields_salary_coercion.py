"""Shared salary-amount coercion: ``comp.coerce_amount`` and its use across providers.

Covers the Stage-1 follow-up that extracted the near-identical amount parsers in
recruitee/teamtailor/join into one helper, plus join's zero-decimal-currency divisor fix
(join amounts are minor units / 100, but zero-decimal ISO-4217 currencies like JPY have no
minor unit).
"""

from ergon_tracker.extract.comp import coerce_amount
from ergon_tracker.providers.join import _minor_amount

# --- coerce_amount --------------------------------------------------------------


def test_coerce_amount_accepts_int() -> None:
    assert coerce_amount(5200) == 5200.0


def test_coerce_amount_accepts_float() -> None:
    assert coerce_amount(53000.5) == 53000.5


def test_coerce_amount_accepts_numeric_string() -> None:
    assert coerce_amount("5200") == 5200.0


def test_coerce_amount_accepts_thousands_comma_string() -> None:
    assert coerce_amount("53,000") == 53000.0


def test_coerce_amount_rejects_nan_string() -> None:
    assert coerce_amount("nan") is None


def test_coerce_amount_rejects_inf_string() -> None:
    assert coerce_amount("inf") is None


def test_coerce_amount_rejects_neg_inf_string() -> None:
    assert coerce_amount("-inf") is None


def test_coerce_amount_rejects_empty_string() -> None:
    assert coerce_amount("") is None


def test_coerce_amount_rejects_none() -> None:
    assert coerce_amount(None) is None


def test_coerce_amount_rejects_non_numeric_string() -> None:
    assert coerce_amount("abc") is None


def test_coerce_amount_rejects_bool() -> None:
    assert coerce_amount(True) is None
    assert coerce_amount(False) is None


def test_coerce_amount_rejects_zero() -> None:
    assert coerce_amount(0) is None


def test_coerce_amount_rejects_negative() -> None:
    assert coerce_amount(-5) is None


# --- join zero-decimal currency divisor -----------------------------------------


def test_join_minor_amount_zero_decimal_currency_not_divided_by_100() -> None:
    # JPY has no minor unit: 5,000,000 is already whole yen, not cents.
    amount, currency = _minor_amount({"amount": 5000000, "currency": "JPY"})
    assert amount == 5000000.0
    assert currency == "JPY"


def test_join_minor_amount_two_decimal_currency_divided_by_100() -> None:
    amount, currency = _minor_amount({"amount": 18000000, "currency": "EUR"})
    assert amount == 180000.0
    assert currency == "EUR"
