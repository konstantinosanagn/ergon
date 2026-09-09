"""Unit tests for the PeopleSoft Candidate Gateway provider.

The live fetch flow uses a stateful curl_cffi session (TLS-impersonation + ICAction postbacks) that
can't be respx-mocked, so these tests cover the parsing/normalize logic — where the complexity is —
against a fixture grid, plus token parsing.
"""

from __future__ import annotations

from ergon.models import RawJob, make_job_id
from ergon.providers.peoplesoft import PeopleSoftProvider

# A trimmed PeopleSoft results grid: two job rows (indices 0,1) across the parallel id-indexed spans,
# one with a nested markup title and an apostrophe/ampersand.
GRID = """
<input type='hidden' id='ICSID' value='abc123XYZ' />
<input type='hidden' id='ICStateNum' value='4' />
<span id='SCH_JOB_TITLE$0' class='ps'>Nurse Practitioner <b>SC</b> (Dual &amp; Posting)</span>
<span id='SCH_JOB_TITLE$1' class='ps'>Academic Advisor</span>
<span id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$0'>60051</span>
<span id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$1'>2959345</span>
<span id='LOCATION$0'>Columbia</span>
<span id='LOCATION$1'>Fargo</span>
<span id='HRS_APP_JBSCH_I_HRS_DEPT_DESCR$0'>Neurology</span>
<span id='HRS_APP_JBSCH_I_HRS_DEPT_DESCR$1'>Student Affairs</span>
<span id='HRS_BU_DESCR$0'>University of Missouri</span>
<span id='HRS_BU_DESCR$1'>North Dakota State University</span>
<span id='SCH_OPENED$0'>05/30/2026</span>
<span id='SCH_OPENED$1'>06/01/2026</span>
"""


def test_parse_token() -> None:
    t = PeopleSoftProvider._parse_token(
        "erecruit.umsystem.edu|tamext|COLUM|6|B|University of Missouri System"
    )
    assert t["host"] == "erecruit.umsystem.edu"
    assert t["site"] == "tamext" and t["node"] == "COLUM"
    assert t["siteid"] == "6" and t["shape"] == "B"
    assert t["company"] == "University of Missouri System" and t["bu_filter"] == ""
    # shape A, no siteid, with a bu_filter trailing field; node defaults to EMPLOYEE
    t2 = PeopleSoftProvider._parse_token("prd.hcm.ndus.edu|recruit||| A |NDSU|North Dakota State")
    assert t2["node"] == "EMPLOYEE" and t2["shape"] == "A" and t2["siteid"] == ""
    assert t2["bu_filter"] == "North Dakota State"


def test_parse_grid() -> None:
    rows = PeopleSoftProvider._parse_grid(GRID)
    assert len(rows) == 2
    r0 = rows[0]
    assert r0["title"] == "Nurse Practitioner SC (Dual & Posting)"  # tags stripped, entity decoded
    assert r0["id"] == "60051"
    assert r0["location"] == "Columbia"
    assert r0["department"] == "Neurology"
    assert r0["business_unit"] == "University of Missouri"
    assert rows[1]["id"] == "2959345"


def test_normalize_builds_apply_url_and_fields() -> None:
    prov = PeopleSoftProvider()
    rec = PeopleSoftProvider._parse_grid(GRID)[0]
    t = PeopleSoftProvider._parse_token(
        "erecruit.umsystem.edu|tamext|COLUM|6|B|University of Missouri System"
    )
    raw = prov._to_raw(rec, t, rec["id"], "tok")
    job = prov.normalize(raw)
    assert job.id == make_job_id("peoplesoft", "60051")
    assert job.company == "University of Missouri System"
    assert job.title == "Nurse Practitioner SC (Dual & Posting)"
    assert job.locations[0].raw == "Columbia"
    assert job.department == "Neurology"
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert "JobOpeningId=60051" in job.apply_url and "SiteId=6" in job.apply_url


def test_matches_is_seed_only() -> None:
    assert PeopleSoftProvider.matches("https://erecruit.umsystem.edu/") is None


def test_to_raw_roundtrip_keeps_payload() -> None:
    rec = {"title": "X", "id": "9", "location": "Remote"}
    raw = RawJob(
        source="peoplesoft", source_job_id="9", company="C", token="t", url="u", payload=rec
    )
    job = PeopleSoftProvider().normalize(raw)
    assert job.remote.value == "remote"
