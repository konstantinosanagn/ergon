"""Tests for the H-1B sponsor adjudicator (the judge) — pure, no network."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_sponsors_tavily import adjudicate, board_of, to_candidate  # noqa: E402


def test_dead_board_always_rejected() -> None:
    ok, _ = adjudicate("google", "greenhouse", "google", "Google", live=False)
    assert ok is False


def test_workday_accepts_matching_tenant() -> None:
    assert adjudicate("google", "workday", "google|wd501|GOCJobs", "", live=True)[0] is True
    assert adjudicate("wal mart associates", "workday", "walmart|wd504|x", "", live=True)[0] is True
    assert adjudicate("citibank n a", "workday", "citi|wd5|2", "", live=True)[0] is True


def test_workday_rejects_wrong_tenant() -> None:
    # the classic false positives: search returned a *different* company's Workday board
    assert adjudicate("cognizant", "workday", "collaborative|wd1|x", "", live=True)[0] is False
    assert adjudicate("microsoft", "workday", "shi|wd12|x", "", live=True)[0] is False
    # apple (Inc) must NOT match Apple Bank's workday tenant
    assert adjudicate("apple", "workday", "applebank|wd5|x", "", live=True)[0] is False


def test_nonworkday_uses_display_company_name() -> None:
    # display name confirms the sponsor
    assert adjudicate("infosys", "smartrecruiters", "Infosys4", "Infosys", live=True)[0] is True
    assert (
        adjudicate("hcl america", "smartrecruiters", "HCLAmericaInc", "HCL America Inc", live=True)[
            0
        ]
        is True
    )
    # display name contradicts -> reject (MrApple board reads "Mr Apple", not Apple)
    assert adjudicate("apple", "smartrecruiters", "MrApple", "Mr Apple", live=True)[0] is False
    # a board literally named "Apple Bank" must not satisfy sponsor "apple"
    assert (
        adjudicate("apple", "workable", "applebank", "Apple Bank for Savings", live=True)[0]
        is False
    )


def test_board_of_maps_urls() -> None:
    assert board_of("https://boards.greenhouse.io/stripe") == ("greenhouse", "stripe")
    assert board_of("https://google.wd501.myworkdayjobs.com/en-US/GOCJobs")[0] == "workday"
    assert board_of("https://example.com/careers") is None


def test_board_of_maps_enterprise_atses() -> None:
    # Expanded lane: enterprise ATSes where many Fortune-500/large-caps live.
    assert board_of("https://acme.bamboohr.com/careers") == ("bamboohr", "acme")
    assert board_of("https://acme.recruitee.com/o/role") == ("recruitee", "acme")
    assert board_of("https://acme.teamtailor.com/jobs") == ("teamtailor", "acme")
    assert board_of("https://acme.jobs.personio.de/job/1") == ("personio", "acme")
    assert board_of("https://careers-pfizer.icims.com/jobs")[0] == "icims"


def test_to_candidate_splits_workday() -> None:
    assert to_candidate("greenhouse", "stripe") == {
        "company": "stripe",
        "ats": "greenhouse",
        "token": "stripe",
        "domain": None,
    }
    assert to_candidate("workday", "google|wd501|GOCJobs") == {
        "company": "google",
        "ats": "workday",
        "tenant": "google",
        "wd": "wd501",
        "site": "GOCJobs",
        "domain": None,
    }
