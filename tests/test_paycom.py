"""Unit tests for the Paycom provider — matches() + normalize() (pure; fetch is browser-backed)."""

from __future__ import annotations

from ergon.models import RawJob
from ergon.providers.paycom import PaycomProvider

KEY = "7C5AC05D8D2EC046AE4FAF26F5F9712E"


def test_matches_jobs_and_portal_urls() -> None:
    m = PaycomProvider.matches
    assert (
        m(f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={KEY}&fromClientSide=true")
        == KEY
    )
    assert m(f"https://www.paycomonline.net/v4/ats/web.php/portal/{KEY}/career-page") == KEY
    assert (
        m(
            f"https://www.paycomonline.net/v4/ats/web.php/jobs?jobSearchSettingsId=595&clientkey={KEY}"
        )
        == KEY
    )
    # lowercase clientkey is normalised to upper
    assert m(f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={KEY.lower()}") == KEY


def test_matches_rejects_non_paycom() -> None:
    assert PaycomProvider.matches("https://boards.greenhouse.io/acme") is None
    assert PaycomProvider.matches("https://www.paycomonline.net/v4/ats/web.php/login") is None


def test_parse_carries_company() -> None:
    assert PaycomProvider._parse(KEY) == (KEY, None)
    assert PaycomProvider._parse(f"{KEY}|CFBank") == (KEY, "CFBank")


def test_normalize_fields() -> None:
    raw = RawJob(
        source="paycom",
        source_job_id="207416",
        company="CFBank",
        token=f"{KEY}|CFBank",
        url=f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={KEY}&jobId=207416",
        payload={
            "jobId": 207416,
            "jobTitle": "Sr. Relationship Manager ",
            "locations": "Cincinnati BA - Blue Ash, OH 45242",
            "remoteType": "",
            "description": "About CFBank: ...",
        },
    )
    job = PaycomProvider().normalize(raw)
    assert job.title == "Sr. Relationship Manager"
    assert job.company == "CFBank"
    assert job.locations[0].region == "OH"
    assert "jobId=207416" in job.apply_url
