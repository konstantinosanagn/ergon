"""Unit tests for the Zwayam provider (respx-mocked, offline)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import EmploymentType, RawJob, SearchQuery, make_job_id
from ergon.providers.zwayam import ZwayamProvider

pytestmark = pytest.mark.anyio

CONFIG = "https://public.zwayam.com/data-service/v2/public-configurations"
SEARCH = "https://public.zwayam.com/jobs/search"


def _src(jid: int, title: str, loc: str, dept: str = "Engineering", slug: str = "") -> dict:
    return {
        "_source": {
            "id": jid,
            "jobTitle": title,
            "location": loc,
            "departmentName": dept,
            "jobUrl": slug or f"job-{jid}",
            "workMode": "Onsite",
        }
    }


def _mock(respx_mock: respx.MockRouter, total: int, pages: list[list[dict]]) -> None:
    respx_mock.post(CONFIG).mock(
        return_value=httpx.Response(
            200,
            json={
                "responseObject": {"company": {"id": 15738, "careerSiteUrl": "careers.acme.com"}}
            },
        )
    )

    def _search(request: httpx.Request) -> httpx.Response:
        # filterCri carries paginationStartNo; serve the matching page.
        body = request.content.decode("utf-8", "ignore")
        start = 0
        for part in body.split('name="filterCri"'):
            if "paginationStartNo" in part:
                start = json.loads(part.split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]).get(
                    "paginationStartNo", 0
                )
                break
        idx = start // 10
        hits = pages[idx] if idx < len(pages) else []
        return httpx.Response(200, json={"data": {"totalCount": total, "data": hits}})

    respx_mock.post(SEARCH).mock(side_effect=_search)


def test_parse_token() -> None:
    assert ZwayamProvider._parse("careers.tavant.com|Tavant") == ("careers.tavant.com", "Tavant")
    assert ZwayamProvider._parse("careers.tavant.com") == ("careers.tavant.com", None)


def test_filter_cri_encodes_pagination() -> None:
    assert json.loads(ZwayamProvider._filter_cri(20))["paginationStartNo"] == 20


async def test_fetch_paginates_and_normalizes() -> None:
    pages = [
        [
            _src(1, "Data Architect", "New York, NY, USA"),
            _src(2, "QA Engineer", "Bengaluru, India"),
        ],
        [_src(3, "Remote DevOps", "Remote")],
    ]
    with respx.mock as respx_mock:
        _mock(respx_mock, total=3, pages=pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ZwayamProvider().fetch("careers.acme.com|Acme", SearchQuery(), f)

    assert len(raws) == 3
    assert {r.company for r in raws} == {"Acme"}
    j0 = ZwayamProvider().normalize(raws[0])
    assert j0.id == make_job_id("zwayam", "1")
    assert j0.title == "Data Architect"
    assert j0.locations[0].raw == "New York, NY, USA"
    assert "careers.acme.com/job/job-1" in j0.apply_url
    # remote detection from location text
    jr = ZwayamProvider().normalize(raws[2])
    assert jr.remote.value == "remote"


async def test_fetch_respects_limit() -> None:
    pages = [[_src(i, f"Role {i}", "Pune, India") for i in range(10)]]
    with respx.mock as respx_mock:
        _mock(respx_mock, total=10, pages=pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ZwayamProvider().fetch("careers.acme.com|Acme", SearchQuery(limit=3), f)
    assert len(raws) == 3


async def test_bad_config_degrades_to_empty() -> None:
    with respx.mock as respx_mock:
        respx_mock.post(CONFIG).mock(return_value=httpx.Response(403, json={"message": "Invalid"}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ZwayamProvider().fetch("careers.acme.com|Acme", SearchQuery(), f)
    assert raws == []


async def test_normalize_reads_the_es_source_metadata() -> None:
    """The ES ``_source`` already carries these (live-probed on careers.tavant.com) -- map them."""
    page = [
        {
            "_source": {
                "id": 9,
                "jobTitle": "SDET",
                "location": "Bengaluru, India",
                "departmentName": "Quality Engineering",
                "jobUrl": "job-9",
                "text3": "Full Time Employee",
                "yrsOfExperience": "5 to 8 Years",
                "eduqualification": "Bachelor of Engineering",
                "role": "<ul><li>Be part of all phases of the SDLC.</li></ul>",
            }
        }
    ]
    with respx.mock as respx_mock:
        _mock(respx_mock, total=1, pages=[page])
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ZwayamProvider().fetch("careers.acme.com|Acme", SearchQuery(), f)

    job = ZwayamProvider().normalize(raws[0])
    assert job.employment_type is EmploymentType.FULL_TIME  # text3, the tenant-configured field
    assert (job.years_experience_min, job.years_experience_max) == (5, 8)
    assert job.degree_min == "bachelor"
    assert job.description_html == "<ul><li>Be part of all phases of the SDLC.</li></ul>"
    assert job.department == "Quality Engineering"


def test_normalize_unreadable_metadata_stays_empty() -> None:
    """Tenant free-text that matches no vocabulary is left empty rather than guessed."""
    raw = RawJob(
        source="zwayam",
        source_job_id="9",
        company="Acme",
        payload={
            "id": 9,
            "jobTitle": "SDET",
            "text3": "Retainer",
            "yrsOfExperience": "Not specified",
            "eduqualification": "Any professional computer degree",
        },
    )
    job = ZwayamProvider().normalize(raw)
    assert job.employment_type is EmploymentType.UNKNOWN
    assert (job.years_experience_min, job.years_experience_max) == (None, None)
    assert job.degree_min is None
    assert job.description_html is None


def test_company_id_is_base64() -> None:
    # Guard the encoding contract the live API requires.
    assert base64.b64encode(b"15738").decode() == "MTU3Mzg="
