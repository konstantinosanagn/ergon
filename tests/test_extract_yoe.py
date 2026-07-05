"""Offline tests for the years-of-experience (YoE) extractor."""

from __future__ import annotations

import pytest

from ergon_tracker.extract import get_extractor
from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.yoe import YoeExtractor


def _yoe(
    description: str | None = None, title: str = "Software Engineer"
) -> tuple[int | None, int | None]:
    return YoeExtractor().extract(ExtractInput(title=title, description_text=description))


# --- single minimums --------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We need 5+ years of experience.", (5, None)),
        ("Requires 5+ yrs experience.", (5, None)),
        ("Looking for 5 years+ of professional experience.", (5, None)),
        ("Candidates with at least 5 years of experience.", (5, None)),
        ("A minimum of 5 years of experience is required.", (5, None)),
        ("Minimum 5 years' experience in backend systems.", (5, None)),
        ("5 years minimum of relevant experience.", (5, None)),
        ("More than 7 years of industry experience.", (7, None)),
        ("Over 3 years working with distributed systems.", (3, None)),
        ("8 years of experience building web apps.", (8, None)),
    ],
)
def test_single_minimums(text: str, expected: tuple[int | None, int | None]) -> None:
    assert _yoe(text) == expected


# --- ranges -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3-5 years of experience required.", (3, 5)),
        ("3 to 5 years of professional experience.", (3, 5)),
        ("Between 3 and 5 years of experience.", (3, 5)),
        ("We want 2–4 years experience in data engineering.", (2, 4)),
        ("Seeking someone with 4 to 6 years of experience.", (4, 6)),
    ],
)
def test_ranges(text: str, expected: tuple[int | None, int | None]) -> None:
    assert _yoe(text) == expected


# --- word numbers -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("five years of experience preferred.", (5, None)),
        ("minimum of seven years of experience.", (7, None)),
        ("ten+ years of experience leading teams.", (10, None)),
        ("At least twenty years of experience.", (20, None)),
        ("three to five years of experience.", (3, 5)),
        ("fifteen years of engineering experience.", (15, None)),
    ],
)
def test_word_numbers(text: str, expected: tuple[int | None, int | None]) -> None:
    assert _yoe(text) == expected


# --- upper bounds -----------------------------------------------------------


def test_up_to_is_max_only() -> None:
    assert _yoe("Up to 5 years of experience considered.") == (None, 5)


# --- false-positive guards --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We have driven growth over the last 5 years of growth.",
        "The company was founded 10 years ago.",
        "That happened 5 years ago.",
        "Equity follows a 4-year vesting schedule.",
        "Standard 4 year vesting with a 1 year cliff.",
        "The candidate is 24 years old.",
        "We match your 401k contributions.",
        "Our office lease runs for 10 years.",
        "We have grown for the last 7 years.",
        "This product has been running for 3 years.",
    ],
)
def test_false_positives_return_none(text: str) -> None:
    assert _yoe(text) == (None, None)


def test_no_signal_returns_none() -> None:
    assert _yoe("Great team, competitive salary, free lunch.") == (None, None)


def test_empty_description_and_title() -> None:
    assert YoeExtractor().extract(ExtractInput(title="", description_text=None)) == (None, None)


# --- description vs title precedence / fallback -----------------------------


def test_description_preferred_over_title() -> None:
    out = _yoe(
        description="We require 3-5 years of experience.",
        title="Senior Engineer (10+ years experience)",
    )
    assert out == (3, 5)


def test_falls_back_to_title() -> None:
    out = _yoe(description="Join our growing team!", title="Engineer with 5+ years experience")
    assert out == (5, None)


def test_no_description_uses_title() -> None:
    assert _yoe(description=None, title="Engineer, minimum 6 years of experience") == (6, None)


# --- picks the primary (first / minimum) requirement ------------------------


def test_picks_first_requirement() -> None:
    text = (
        "Required: 4+ years of experience. Preferred: 8+ years of experience in a leadership role."
    )
    assert _yoe(text) == (4, None)


def test_range_chosen_before_later_mention() -> None:
    text = "You should have 3 to 5 years of experience; 10 years is a bonus."
    assert _yoe(text) == (3, 5)


# --- in-field cue without the word "experience" -----------------------------


def test_in_field_cue() -> None:
    assert _yoe("6 years in software engineering roles.") == (6, None)


def test_future_timeframe_not_matched() -> None:
    # "in 5 years" is a future timeframe, not experience.
    assert _yoe("We aim to triple revenue in 5 years.") == (None, None)


# --- recall pass: YOE unit, months, degree pairing, alternation ladders ------


@pytest.mark.parametrize(
    "text,expected",
    [
        # "YOE" is a self-cueing unit (it spells out years-of-experience)
        ("3+ YOE required.", (3, None)),
        ("2 YOE", (2, None)),
        ("5 yoe in backend development", (5, None)),
        # "N yrs' exp" style
        ("5 yrs' exp in backend systems.", (5, None)),
        ("7+ yrs exp.", (7, None)),
        # months convert to years, rounded DOWN
        ("18 months of experience required.", (1, None)),
        ("6 months of professional experience.", (0, None)),
        ("At least 24 months of hands-on experience.", (2, None)),
        ("12 to 36 months of industry experience.", (1, 3)),
        # degree + years pairing reads as a requirement (no explicit experience cue needed)
        ("Bachelor's degree and 5 years in fintech.", (5, None)),
        ("MS degree plus 3 years in data engineering.", (3, None)),
    ],
)
def test_recall_additions(text: str, expected: tuple[int | None, int | None]) -> None:
    assert _yoe(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # degree-alternation ladder -> MINIMUM years across the arms: the lowest barrier a
        # candidate can clear is the posting's true minimum (PhD + 2 => a 2-year floor).
        ("BS + 8 years OR MS + 5 years OR PhD + 2 years.", (2, None)),
        (
            "BS with 8+ years of experience, MS with 5+ years, or PhD with 2+ years.",
            (2, None),
        ),
        (
            "Bachelor's degree and 10 years of experience, or Master's degree and 6 years.",
            (6, None),
        ),
        # single degree-paired phrase: no ladder, value passes through unchanged
        ("PhD and 2 years of postdoctoral experience.", (2, None)),
    ],
)
def test_degree_alternation_takes_minimum(
    text: str, expected: tuple[int | None, int | None]
) -> None:
    assert _yoe(text) == expected


def test_alternation_does_not_hijack_unrelated_later_mentions() -> None:
    # A degree-paired first phrase followed by a DISTANT second phrase is not a ladder.
    text = (
        "BS and 8 years of experience required. "
        "Our founding team spent their careers building infrastructure and developer platforms "
        "across many companies; ideally you have 2 years of experience with Kubernetes."
    )
    assert _yoe(text) == (8, None)


@pytest.mark.parametrize(
    "text",
    [
        # age gates
        "Must be 18 years of age.",
        "Applicants must be 21 years or older.",
        # company age
        "25 years in business serving the region.",
        "Our company has been in operation for 30 years.",
        # engagement length, not experience (month-denominated)
        "This is a 6 month contract position.",
        "12 months maternity cover.",
        "A 3 month probation period applies.",
        "A 12 month assignment with possible extension.",
    ],
)
def test_recall_additions_false_positives(text: str) -> None:
    assert _yoe(text) == (None, None)


# --- registry wiring --------------------------------------------------------


def test_registered_under_name() -> None:
    extractor = get_extractor("yoe")
    assert extractor is not None
    assert extractor.name == "yoe"
    assert extractor.extract(ExtractInput(title="x", description_text="5+ years experience")) == (
        5,
        None,
    )
