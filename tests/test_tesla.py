"""Unit tests for the Tesla careers provider (offline — parses the denormalized state payload)."""

from __future__ import annotations

from ergon.providers.tesla import TeslaProvider

_STATE = {
    "lookup": {
        "locations": {"401022": "Palo Alto, California", "9": "Remote"},
        "departments": {"5": "Tesla AI", "10": "Charging"},
        "types": {"1": "fulltime", "3": "intern"},
    },
    "listings": [
        {"id": "224501", "t": "AI Engineer, Optimus", "dp": "5", "l": "401022", "y": 1},
        {"id": "224501", "t": "AI Engineer, Optimus", "dp": "5", "l": "401022", "y": 1},  # dup id
        {"id": "300100", "t": "Service Intern", "dp": "10", "l": "9", "y": 3},
        {"id": "", "t": "no id — skipped"},
    ],
}


def test_matches() -> None:
    assert TeslaProvider.matches("https://www.tesla.com/careers/search/") == "tesla"
    assert TeslaProvider.matches("https://www.tesla.com/cua-api/apps/careers/state") == "tesla"
    assert TeslaProvider.matches("https://www.teslamotors.example.com/jobs") is None
    assert TeslaProvider.matches("https://greenhouse.io/tesla") is None


def test_parse_resolves_lookup_and_dedupes() -> None:
    raws = TeslaProvider._raws_from_state(_STATE, "tesla", None)
    assert len(raws) == 2  # dup id collapsed, empty id dropped
    assert {r.source_job_id for r in raws} == {"224501", "300100"}
    assert all(r.company == "Tesla" for r in raws)
    j0 = TeslaProvider().normalize(raws[0])
    assert j0.title == "AI Engineer, Optimus"
    assert j0.locations[0].raw == "Palo Alto, California"
    assert j0.department == "Tesla AI"
    assert j0.employment_type.value == "full_time"
    assert j0.apply_url == "https://www.tesla.com/careers/search/job/224501"


def test_remote_and_intern_resolution() -> None:
    raws = TeslaProvider._raws_from_state(_STATE, "tesla", None)
    intern = next(r for r in raws if r.source_job_id == "300100")
    j = TeslaProvider().normalize(intern)
    assert j.remote.value == "remote"
    assert j.employment_type.value == "internship"


def test_limit_respected() -> None:
    raws = TeslaProvider._raws_from_state(_STATE, "tesla", 1)
    assert len(raws) == 1


def test_empty_or_bad_payload() -> None:
    assert TeslaProvider._raws_from_state({}, "tesla", None) == []
    assert TeslaProvider._raws_from_state(None, "tesla", None) == []
