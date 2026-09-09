"""Posting-level sponsorship detection (regex) + filter."""

from __future__ import annotations

import pytest

from ergon.enrich import enrich_in_place
from ergon.extract.sponsorship import detect_sponsorship
from ergon.models import JobPosting, SearchQuery


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Applicant must not require sponsorship now or in the future.", False),
        ("We are unable to offer sponsorship at this time.", False),
        ("We do not provide visa sponsorship.", False),
        ("This role is not eligible for visa sponsorship.", False),
        ("Must be authorized to work in the US without sponsorship.", False),
        ("Employer will not sponsor applicants for work visas.", False),
        ("No visa sponsorship is provided for this position.", False),
        ("No immigration sponsorship available", False),  # real Ecolab case (regression)
        ("must be authorized to work in the U.S. now and in the future without sponsorship", False),
        ("Visa sponsorship is available for this role.", True),
        ("Sponsorship welcome!", True),
        ("We will sponsor the right candidate.", True),
        ("H-1B sponsorship available for qualified applicants.", True),
        ("We are happy to provide visa sponsorship.", True),
        ("Open to sponsorship for exceptional candidates.", True),
        ("Candidates requiring sponsorship are welcome to apply.", True),
        ("Great team, competitive salary, free lunch.", None),
        ("You must have authorization to work in the United States.", None),  # no 'sponsor' cue
        ("", None),
        (None, None),
    ],
)
def test_detect_sponsorship(text: str | None, expected: bool | None) -> None:
    assert detect_sponsorship(text) is expected


def _job(desc: str | None) -> JobPosting:
    return JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Engineer",
        description_text=desc,
    )


def test_enrich_sets_sponsorship_offered() -> None:
    yes = _job("Visa sponsorship available.")
    no = _job("Must not require sponsorship now or in the future.")
    unk = _job("We value diversity and growth.")
    for j in (yes, no, unk):
        enrich_in_place(j)
    assert yes.sponsorship_offered is True
    assert no.sponsorship_offered is False
    assert unk.sponsorship_offered is None


def test_filter_keeps_unknown_by_default_drops_explicit_no() -> None:
    yes, no, unk = _job("x"), _job("x"), _job("x")
    yes.sponsorship_offered, no.sponsorship_offered, unk.sponsorship_offered = True, False, None
    q = SearchQuery(sponsorship_offered=True)  # default include_unknown_sponsorship=True
    assert q.matches(yes) is True
    assert q.matches(unk) is True  # unstated kept (the majority)
    assert q.matches(no) is False  # explicit "no sponsorship" dropped


def test_filter_strict_drops_unknown_when_opted_out() -> None:
    unk = _job("x")
    unk.sponsorship_offered = None
    q = SearchQuery(sponsorship_offered=True, include_unknown_sponsorship=False)
    assert q.matches(unk) is False  # strict: only explicit yes survives


def test_no_filter_keeps_everything() -> None:
    no = _job("x")
    no.sponsorship_offered = False
    assert SearchQuery().matches(no) is True
