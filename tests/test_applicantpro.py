"""Unit tests for the ApplicantPro provider (respx-mocked, offline).

Mirrors the real public endpoint discovered by capturing the careers SPA's XHR:
``GET /core/jobs/{domainId}?getParams=...`` -> {"data": {"jobs": [...]}}, with the tenant domainId
discoverable from the careers HTML when the registry token omits it."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, SearchQuery
from ergon_tracker.providers.applicantpro import ApplicantProProvider

pytestmark = pytest.mark.anyio

_API = r".*/core/jobs/11099.*"
_CAREERS = "https://acme.applicantpro.com/jobs/"
_RESPONSE = {
    "success": True,
    "data": {
        "jobs": [
            {
                "id": 4080524,
                "title": "Assembler II",
                "city": "St Paul",
                "abbreviation": "MN",
                "iso3": "USA",
                "classification": "Full-Time",
                "orgTitle": "Production",
                "subdomain": "acme",
                "minSalary": 76000,
                "maxSalary": 92500,
                "payType": "Salary",
                "payTypeFrame": "per year",
            },
            {
                "id": 4080525,
                "title": "Process Engineer",
                "city": "Holliston",
                "classification": "Part-Time",
                "orgTitle": "Engineering",
            },
            {"id": 0, "title": "", "city": ""},  # junk row -> must be dropped (no id/title)
        ]
    },
}


def test_matches_recognizes_hosts() -> None:
    p = ApplicantProProvider
    assert p.matches("https://harvardbioscience.applicantpro.com/jobs/") == "harvardbioscience"
    assert p.matches("genasys.applicantpro.com") == "genasys"
    assert p.matches("https://www.applicantpro.com") is None  # www is not a tenant
    assert p.matches("jobs.applicantpro.com") is None
    assert p.matches("https://acme.greenhouse.io") is None


async def test_fetch_with_domain_id_in_token_skips_discovery() -> None:
    with respx.mock:
        api = respx.get(url__regex=_API).mock(return_value=httpx.Response(200, json=_RESPONSE))
        careers = respx.get(_CAREERS).mock(
            return_value=httpx.Response(200, text="should-not-be-called")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ApplicantProProvider().fetch("acme|11099", SearchQuery(), f)
        assert api.called and not careers.called  # token carried the id -> no discovery GET
    assert [r.source_job_id for r in raws] == ["4080524", "4080525"]  # junk row dropped
    assert raws[0].company == "acme" and raws[0].token == "acme|11099"
    assert raws[0].url == "https://acme.applicantpro.com/jobs/4080524"


async def test_fetch_discovers_domain_id_from_careers_html() -> None:
    html = "<script>var cfg = {domain_id: 11099, subdomain:'acme'};</script>"
    with respx.mock:
        careers = respx.get(_CAREERS).mock(return_value=httpx.Response(200, text=html))
        respx.get(url__regex=_API).mock(return_value=httpx.Response(200, json=_RESPONSE))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ApplicantProProvider().fetch("acme", SearchQuery(), f)
        assert careers.called  # had to discover
    assert len(raws) == 2
    assert raws[0].token == "acme|11099"  # canonicalized so the next crawl skips discovery


async def test_normalize_maps_fields() -> None:
    with respx.mock:
        respx.get(url__regex=_API).mock(return_value=httpx.Response(200, json=_RESPONSE))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ApplicantProProvider().fetch("acme|11099", SearchQuery(), f)
    j0 = ApplicantProProvider().normalize(raws[0])
    assert j0.title == "Assembler II"
    assert j0.locations[0].city == "St Paul" and j0.locations[0].country == "USA"
    assert j0.department == "Production"
    assert j0.employment_type == EmploymentType.FULL_TIME
    assert j0.apply_url == "https://acme.applicantpro.com/jobs/4080524"
    # structured pay from the list payload (minSalary/maxSalary/payTypeFrame)
    from ergon_tracker.models import SalaryInterval

    assert j0.salary is not None
    assert j0.salary.min_amount == 76_000 and j0.salary.max_amount == 92_500
    assert j0.salary.currency == "USD" and j0.salary.interval is SalaryInterval.YEAR
    j1 = ApplicantProProvider().normalize(raws[1])
    assert j1.employment_type == EmploymentType.PART_TIME
    assert j1.salary is None  # no pay fields -> None (enrich can body-extract)


async def test_empty_and_malformed_return_no_jobs() -> None:
    for payload in ({"success": True, "data": {"jobs": []}}, {"data": {}}, {"oops": 1}, []):
        with respx.mock:
            respx.get(url__regex=_API).mock(return_value=httpx.Response(200, json=payload))
            async with AsyncFetcher(per_host_rate=100) as f:
                raws = await ApplicantProProvider().fetch("acme|11099", SearchQuery(), f)
        assert raws == []


async def test_discovery_failure_returns_empty() -> None:
    with respx.mock:
        respx.get(_CAREERS).mock(return_value=httpx.Response(200, text="<html>no id here</html>"))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ApplicantProProvider().fetch("acme", SearchQuery(), f)
    assert raws == []  # no domainId discoverable -> no fetch, no crash
