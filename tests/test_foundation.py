"""Foundation tests: canonical models + AsyncFetcher. Mirror this pattern in provider tests."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker import (
    EmploymentType,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SearchQuery,
    SearchResult,
)
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import make_job_id

pytestmark = pytest.mark.anyio


def test_make_job_id_is_stable_and_short() -> None:
    a = make_job_id("greenhouse", "123")
    b = make_job_id("greenhouse", "123")
    c = make_job_id("lever", "123")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_jobposting_create_fills_id_and_provenance() -> None:
    job = JobPosting.create(
        source="greenhouse",
        source_job_id=42,
        company="Acme",
        title="Backend Engineer",
        apply_url="https://x/apply",
    )
    assert job.id == make_job_id("greenhouse", "42")
    assert job.source_job_id == "42"
    assert len(job.provenance) == 1
    assert job.provenance[0].source == "greenhouse"
    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.UNKNOWN


def test_searchquery_matches_keywords_location_remote() -> None:
    job = JobPosting.create(
        source="s",
        source_job_id="1",
        company="Acme",
        title="Senior Backend Engineer",
        locations=[Location(city="Berlin", country="DE")],
        remote=RemoteType.REMOTE,
        salary=Salary(min_amount=100_000, currency="EUR"),
    )
    assert SearchQuery(keywords="backend engineer").matches(job)
    assert SearchQuery(keywords="backend", location="berlin", remote=True).matches(job)
    assert not SearchQuery(keywords="data scientist").matches(job)
    assert not SearchQuery(location="paris").matches(job)


def test_searchresult_iter_len_and_dicts() -> None:
    job = JobPosting.create(source="s", source_job_id="1", company="A", title="T")
    result = SearchResult(jobs=[job])
    assert len(result) == 1
    assert [j.title for j in result] == ["T"]
    assert result.to_dicts()[0]["company"] == "A"


async def test_fetcher_get_json_ok() -> None:
    with respx.mock:
        respx.get("https://api.test/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": [1, 2]})
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            data = await f.get_json("https://api.test/jobs")
        assert data == {"jobs": [1, 2]}


async def test_fetcher_retries_on_429_then_succeeds() -> None:
    with respx.mock:
        route = respx.get("https://api.test/flaky")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
        async with AsyncFetcher(per_host_rate=100, retries=4) as f:
            data = await f.get_json("https://api.test/flaky")
        assert data == {"ok": True}
        assert route.call_count == 2
