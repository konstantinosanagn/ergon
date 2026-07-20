"""Unit tests for the Workday provider (offline, respx-mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import RawJob, RemoteType, SearchQuery
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.workday import WorkdayProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


pytestmark = pytest.mark.anyio

JOBS_URL = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
TOKEN = "nvidia|wd5|NVIDIAExternalCareerSite"


def _posting(i: int) -> dict[str, object]:
    return {
        "title": f"Engineer {i}",
        "externalPath": f"/job/US-CA-Santa-Clara/Engineer-{i}_JR{1000 + i}",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted 3 Days Ago",
        "bulletFields": [f"JR{1000 + i}"],
    }


def _paged_handler(total: int) -> tuple[list[int], object]:
    """Return (offsets_seen, side_effect) that serves ``total`` postings, 20 per page."""
    offsets_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset = int(body["offset"])
        offsets_seen.append(offset)
        page = [_posting(i) for i in range(offset, min(offset + 20, total))]
        return httpx.Response(200, json={"total": total, "jobPostings": page})

    return offsets_seen, handler


# --- token / matches --------------------------------------------------------


def test_token_parse_format_round_trip() -> None:
    token = WorkdayProvider.make_token("nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert token == TOKEN
    tenant, wd, site = WorkdayProvider._parse_token(token)
    assert (tenant, wd, site) == ("nvidia", "wd5", "NVIDIAExternalCareerSite")


def test_parse_token_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        WorkdayProvider._parse_token("nvidia|wd5")


def test_matches_on_careers_url() -> None:
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/foo"
    assert WorkdayProvider.matches(url) == TOKEN


def test_matches_on_cxs_url() -> None:
    assert WorkdayProvider.matches(JOBS_URL) == TOKEN


def test_matches_tenant_appears_twice() -> None:
    url = "https://salesforce.wd12.myworkdayjobs.com/salesforce/External_Career_Site"
    assert WorkdayProvider.matches(url) == "salesforce|wd12|External_Career_Site"


def test_matches_site_named_after_tenant() -> None:
    # Regression: Workday sites are very often named after the tenant (``/AAON`` for tenant
    # ``aaon``). The site-picker must NOT mistake that for redundant tenant framing and drop it.
    assert WorkdayProvider.matches("https://aaon.wd108.myworkdayjobs.com/AAON") == "aaon|wd108|AAON"
    assert (
        WorkdayProvider.matches("https://adtran.wd3.myworkdayjobs.com/en-US/ADTRAN")
        == "adtran|wd3|ADTRAN"
    )
    # cxs form where the site also equals the tenant must still resolve.
    assert (
        WorkdayProvider.matches("https://aaon.wd108.myworkdayjobs.com/wday/cxs/aaon/AAON/jobs")
        == "aaon|wd108|AAON"
    )


def test_matches_bare_host_without_site_is_none() -> None:
    assert WorkdayProvider.matches("https://nvidia.wd5.myworkdayjobs.com/") is None
    # cxs framing with the tenant but NO site segment is not a valid board.
    assert (
        WorkdayProvider.matches("https://aaon.wd108.myworkdayjobs.com/wday/cxs/aaon/jobs") is None
    )


def test_matches_rejects_non_workday() -> None:
    assert WorkdayProvider.matches("https://boards.greenhouse.io/acme") is None


# --- fetch / concurrent pagination -----------------------------------------


async def test_fetch_paginates_concurrently_over_total() -> None:
    offsets_seen, handler = _paged_handler(total=45)
    with respx.mock:
        route = respx.post(JOBS_URL)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    # 45 results across pages of 20 → offsets 0, 20, 40 → 3 POSTs, all jobs returned.
    assert route.call_count == 3
    assert sorted(offsets_seen) == [0, 20, 40]
    assert len(raw) == 45
    assert all(isinstance(r, RawJob) for r in raw)
    # Stable ordering by offset preserved.
    assert raw[0].payload["title"] == "Engineer 0"
    assert raw[-1].payload["title"] == "Engineer 44"


async def test_fetch_respects_query_limit_to_cap_pagination() -> None:
    """With a small query.limit we must not pull every page of a huge tenant."""
    offsets_seen, handler = _paged_handler(total=1000)
    with respx.mock:
        route = respx.post(JOBS_URL)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(limit=10), f)

    # limit=10 -> want = max(10, PAGE_SIZE=20) = 20 -> only page 0 fetched.
    assert route.call_count == 1
    assert offsets_seen == [0]
    assert len(raw) == 20


async def test_fetch_caps_pages_at_max_pages() -> None:
    """Without a limit, a huge tenant is bounded by MAX_PAGES instead of MAX_RESULTS."""
    _, handler = _paged_handler(total=100_000)
    with respx.mock:
        route = respx.post(JOBS_URL)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=200) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    assert route.call_count == WorkdayProvider.MAX_PAGES
    assert len(raw) == WorkdayProvider.MAX_PAGES * WorkdayProvider.PAGE_SIZE


async def test_fetch_builds_urls_and_ids() -> None:
    _, handler = _paged_handler(total=2)
    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    job = raw[0]
    assert job.source == "workday"
    assert job.company == "nvidia"
    assert job.token == TOKEN
    assert job.source_job_id == "/job/US-CA-Santa-Clara/Engineer-0_JR1000"
    assert job.url == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        "/job/US-CA-Santa-Clara/Engineer-0_JR1000"
    )


async def test_fetch_passes_keywords_as_search_text() -> None:
    seen_text: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_text.append(json.loads(request.content)["searchText"])
        return httpx.Response(200, json={"total": 1, "jobPostings": [_posting(0)]})

    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            await WorkdayProvider().fetch(TOKEN, SearchQuery(keywords="cuda kernel"), f)

    assert seen_text == ["cuda kernel"]


async def test_fetch_respects_limit_of_20_in_body() -> None:
    seen_limit: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_limit.append(json.loads(request.content)["limit"])
        return httpx.Response(200, json={"total": 1, "jobPostings": [_posting(0)]})

    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    assert seen_limit == [20]


# --- normalize --------------------------------------------------------------


async def test_normalize_maps_fields() -> None:
    _, handler = _paged_handler(total=1)
    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    provider = WorkdayProvider()
    job = provider.normalize(raw[0])
    assert job.title == "Engineer 0"
    assert job.company == "nvidia"
    assert job.source == "workday"
    assert job.apply_url == raw[0].url
    assert job.locations[0].raw == "US, CA, Santa Clara"
    assert job.remote is RemoteType.UNKNOWN
    assert job.posted_at is not None  # "Posted 3 Days Ago" is parseable
    assert job.raw["externalPath"] == raw[0].payload["externalPath"]


def test_normalize_detects_remote() -> None:
    raw = RawJob(
        source="workday",
        source_job_id="/job/x_JR1",
        company="nvidia",
        token=TOKEN,
        url="https://example",
        payload={"title": "Remote Engineer", "locationsText": "Remote, US", "postedOn": "weird"},
    )
    job = WorkdayProvider().normalize(raw)
    assert job.remote is RemoteType.REMOTE
    assert job.posted_at is None  # unparseable postedOn → None, never invented


# --- fixture sanity ---------------------------------------------------------


def test_real_fixture_normalizes() -> None:
    data = json.loads(load_fixture("workday_sample.json"))
    posting = data["jobPostings"][0]
    raw = RawJob(
        source="workday",
        source_job_id=posting["externalPath"],
        company="nvidia",
        token=TOKEN,
        url=f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite{posting['externalPath']}",
        payload=posting,
    )
    job = WorkdayProvider().normalize(raw)
    assert job.title == posting["title"]
    assert job.company == "nvidia"
    assert data["total"] == 2000


# --- fetch_detail: 404-vs-transient hardening contract ----------------------

DETAIL_APPLY_URL = (
    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    "/job/US-CA-Santa-Clara/Engineer-0_JR1000"
)
DETAIL_CXS_URL = (
    "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
    "/job/US-CA-Santa-Clara/Engineer-0_JR1000"
)


class _FakeFetcher:
    """Stands in for AsyncFetcher: returns a fixed payload, or raises a fixed exception."""

    def __init__(self, *, payload: object = None, exc: BaseException | None = None) -> None:
        self._payload = payload
        self._exc = exc
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._payload


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", DETAIL_CXS_URL)
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


def _detail_ref(apply_url: str | None = DETAIL_APPLY_URL) -> DetailRef:
    return DetailRef(
        id="1",
        source="workday",
        token=TOKEN,
        apply_url=apply_url,
        listing_url=None,
        content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await WorkdayProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await WorkdayProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await WorkdayProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await WorkdayProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_description() -> None:
    payload = {"jobPostingInfo": {"jobDescription": "<p>Build chips.</p>"}}
    fetcher = _FakeFetcher(payload=payload)
    res = await WorkdayProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_CXS_URL]
    assert res == "<p>Build chips.</p>"


async def test_fetch_detail_unbuildable_url_raises() -> None:
    # No apply_url/listing_url that resolves to a cxs URL is NOT evidence of death -> raise.
    fetcher = _FakeFetcher(payload={})
    with pytest.raises(RuntimeError):
        await WorkdayProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)


async def test_fetch_detail_unexpected_shape_raises() -> None:
    # A 200 with an unclassifiable shape is indeterminate, not a verified soft-404 -> raise.
    fetcher = _FakeFetcher(payload={"jobPostingInfo": {}})
    with pytest.raises(RuntimeError):
        await WorkdayProvider().fetch_detail(_detail_ref(), fetcher)


# --- board_count: cheap page-1 total change-CANDIDATE signal -----------------------------------


async def test_board_count_reads_uncapped_total() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        return httpx.Response(200, json={"total": 10909, "jobPostings": [_posting(0)]})

    with respx.mock:
        route = respx.post(JOBS_URL)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await WorkdayProvider().board_count(TOKEN, f)

    assert count == 10909
    assert route.call_count == 1  # exactly ONE request


async def test_board_count_404_returns_none() -> None:
    with respx.mock:
        respx.post(JOBS_URL).mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await WorkdayProvider().board_count(TOKEN, f)
    assert count is None


async def test_board_count_transient_error_raises() -> None:
    with respx.mock:
        respx.post(JOBS_URL).mock(return_value=httpx.Response(503))
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            with pytest.raises(TransientHTTPError):
                await WorkdayProvider().board_count(TOKEN, f)


async def test_board_count_missing_total_raises() -> None:
    with respx.mock:
        respx.post(JOBS_URL).mock(return_value=httpx.Response(200, json={"jobPostings": []}))
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(RuntimeError):
                await WorkdayProvider().board_count(TOKEN, f)


async def test_base_provider_board_count_is_none() -> None:
    fetcher = _FakeFetcher(payload={})
    assert await BaseProvider().board_count(TOKEN, fetcher) is None  # type: ignore[arg-type]


# --- board_count: live gate (ERGON_LIVE_TESTS=1) ------------------------------------------------

_PROBE_FILE = Path(
    "/private/tmp/claude-501/-Users-kanagn-Desktop-job-researcher/"
    "d20c6e7c-0b7f-4b04-a828-a75251378b9c/scratchpad/probe_targets.json"
)
_SEED_FILE = Path(__file__).resolve().parents[1] / "src/ergon_tracker/registry/data/seed.json"


def _live_tokens(ats: str, n: int = 3) -> list[str]:
    """Token sample for the ``board_count`` live gate: prefer the investigator's
    ``probe_targets.json`` (pre-verified live boards, if present), else fall back to the registry
    seed filtered by ``ats`` -- mirrors ``tests/live``'s own ``_tokens`` helper."""
    if _PROBE_FILE.exists():
        try:
            data = json.loads(_PROBE_FILE.read_text())
            toks = [e["token"] for e in data.get(ats, []) if e.get("token")]
            if toks:
                return toks[:n]
        except Exception:
            pass
    with open(_SEED_FILE) as f:
        seed = json.load(f)["companies"]
    return [
        e["token"]
        for e in seed.values()
        if isinstance(e, dict) and e.get("ats") == ats and e.get("token")
    ][:n]


@pytest.mark.live
async def test_board_count_live_positive_and_consistent_with_sampled_fetch() -> None:
    tokens = _live_tokens("workday", 5)
    assert tokens, "no workday tokens available (neither probe_targets.json nor seed.json)"
    checked = positive = 0
    async with AsyncFetcher(per_host_rate=5, retries=2) as f:
        for token in tokens:
            try:
                count = await WorkdayProvider().board_count(token, f)
            except Exception:
                continue
            if count is None:
                continue
            assert count >= 0, f"{token}: board_count returned negative {count}"
            sampled = await WorkdayProvider().fetch(token, SearchQuery(limit=20), f)
            assert count >= len(sampled), (
                f"{token}: board_count {count} < sampled fetch {len(sampled)}"
            )
            checked += 1
            positive += count > 0
    assert checked >= 1, "no live workday board yielded a usable board_count"
    assert positive >= 1, "no live workday board yielded a POSITIVE board_count"
