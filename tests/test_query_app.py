"""HTTP QUERY surface (RFC 10008): correctness + concurrency stress, driving the REAL index-backed
search (the SqliteIndexBackend path the MCP uses) over the real QUERY method in-process via httpx
ASGITransport — no network, no server, laptop-safe."""

from __future__ import annotations

import anyio
import httpx
import pytest

from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.models import JobPosting, Location, RemoteType
from ergon_tracker.serve.query_app import QueryApp, SearchService

pytestmark = pytest.mark.anyio


def _job(sid, title, desc="", degree_min=None, country="United States"):
    j = JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=f"Co{sid}",
        title=title,
        description_text=desc,
        locations=[Location(raw=f"City, {country}", city="City", country=country)],
        remote=RemoteType.ONSITE,
    )
    if degree_min:
        j.degree_min = degree_min
        j.degree_required = False
    return j


@pytest.fixture
def index(tmp_path):
    jobs = [
        _job("1", "Registered Nurse", "patient care"),
        _job("2", "Nurse Practitioner", "advanced practice", degree_min="master"),
        _job("3", "Equity Research Associate", "biotech coverage", degree_min="phd_md"),
        _job("4", "Software Engineer", "python services"),
        _job("5", "Lab Technician", "assays", degree_min="bachelor"),
        _job("6", "Data Analyst", "dashboards", country="Canada"),
    ]
    p = tmp_path / "index.sqlite"
    build_index(jobs, p, build_id="build-A")
    return p


def _app(index, **kw):
    return QueryApp(SearchService(index, **kw))


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _query(client, body, method="QUERY", **kw):
    return await client.request(method, "/jobs", json=body, **kw)


# --- correctness ----------------------------------------------------------------------------------
async def test_query_and_post_are_identical(index):
    async with _client(_app(index)) as c:
        q = await _query(c, {"keywords": "nurse"}, method="QUERY")
        p = await _query(c, {"keywords": "nurse"}, method="POST")
    assert q.status_code == p.status_code == 200
    assert q.json()["results"] == p.json()["results"]  # QUERY == POST, same engine
    assert q.json()["count"] >= 1


async def test_filters_apply_degree(index):
    # the degree filter (schema-v2) works over the QUERY surface: max_degree=bachelor excludes the
    # phd_md 'Equity Research Associate' and the master 'Nurse Practitioner'.
    async with _client(_app(index)) as c:
        r = await _query(c, {"max_degree": "bachelor", "limit": 50})
    titles = {j["title"] for j in r.json()["results"]}
    assert "Equity Research Associate" not in titles
    assert "Nurse Practitioner" not in titles
    assert "Software Engineer" in titles  # unspecified degree kept by default


async def test_country_filter(index):
    async with _client(_app(index)) as c:
        r = await _query(c, {"country": "US", "limit": 50})
    assert all("Canada" not in (j["location"] or "") for j in r.json()["results"])


# --- the QUERY payoff: ETag / 304 / body-aware cache ----------------------------------------------
async def test_etag_304_and_cache(index):
    async with _client(_app(index)) as c:
        r1 = await _query(c, {"keywords": "engineer"})
        assert r1.status_code == 200 and r1.headers["x-cache"] == "MISS"
        etag = r1.headers["etag"]
        assert etag and r1.headers["cache-control"].startswith("public")

        r2 = await _query(c, {"keywords": "engineer"})
        assert r2.status_code == 200 and r2.headers["x-cache"] == "HIT"  # served from cache
        assert r2.headers["etag"] == etag

        r3 = await _query(c, {"keywords": "engineer"}, headers={"if-none-match": etag})
        assert r3.status_code == 304 and r3.content == b""  # bodyless Not-Modified
        assert r3.headers["etag"] == etag


async def test_key_order_hits_same_cache_entry(index):
    # reordered body keys must collapse to one cache entry (poison-resistant canonicalization)
    async with _client(_app(index)) as c:
        a = await _query(c, {"keywords": "nurse", "max_degree": "master", "limit": 10})
        b = await _query(c, {"limit": 10, "max_degree": "master", "keywords": "nurse"})
    assert a.headers["etag"] == b.headers["etag"]
    assert b.headers["x-cache"] == "HIT"


async def test_new_build_rotates_etag(index, tmp_path):
    svc = SearchService(index)
    app = QueryApp(svc)
    async with _client(app) as c:
        first = (await _query(c, {"keywords": "nurse"})).headers["etag"]
    # rebuild the same path with a different build_id -> ETag must change (cache invalidation)
    build_index([_job("1", "Registered Nurse", "patient care")], index, build_id="build-B")
    async with _client(app) as c:
        second = (await _query(c, {"keywords": "nurse"})).headers["etag"]
    assert first != second


# --- error handling -------------------------------------------------------------------------------
async def test_error_cases(index):
    async with _client(_app(index)) as c:
        assert (
            await c.request(
                "QUERY", "/jobs", content=b"{bad json", headers={"content-type": "application/json"}
            )
        ).status_code == 400
        assert (await _query(c, {"limit": "not-an-int"})).status_code == 400  # pydantic type error
        assert (await c.request("GET", "/jobs")).status_code == 405
        assert (await c.request("QUERY", "/nope", json={})).status_code == 404
        big = await c.request(
            "QUERY",
            "/jobs",
            content=b"x" * (64 * 1024 + 1),
            headers={"content-type": "application/json"},
        )
        assert big.status_code == 413
        wrong = await c.request(
            "QUERY", "/jobs", content=b"{}", headers={"content-type": "text/plain"}
        )
        assert wrong.status_code == 415
        allow = await c.request("GET", "/jobs")
        assert "QUERY" in allow.headers.get("allow", "")


async def test_semantic_rejected_honestly(index):
    # semantic=true must NOT silently return lexical results — the surface says 501, not a wrong body.
    async with _client(_app(index)) as c:
        r = await _query(c, {"keywords": "nurse", "semantic": True})
    assert r.status_code == 501 and "semantic" in r.json()["error"].lower()


async def test_health(index):
    async with _client(_app(index)) as c:
        r = await c.request("GET", "/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"
    assert body["build_id"] == "build-A" and body["row_count"] >= 1


async def test_empty_body_is_valid_query(index):
    async with _client(_app(index)) as c:
        r = await c.request("QUERY", "/jobs", content=b"")
    assert (
        r.status_code == 200 and r.json()["count"] >= 1
    )  # empty body == match-all (bounded by limit)


# --- concurrency + single-flight (the production-grade core) ---------------------------------------
class _CountingBackend:
    """Wraps the real backend to count searches and inject latency so concurrent misses overlap."""

    def __init__(self, real, delay=0.0):
        self._real = real
        self.calls = 0
        self._delay = delay

    def metadata(self):
        return self._real.metadata()

    def search(self, query):
        self.calls += 1
        if self._delay:
            import time

            time.sleep(self._delay)
        return self._real.search(query)


async def test_single_flight_coalesces_identical_misses(index):
    svc = SearchService(index, max_db_threads=8)
    counting = _CountingBackend(SqliteIndexBackend(index), delay=0.05)
    svc.backend = counting  # type: ignore[assignment]
    app = QueryApp(svc)
    async with _client(app) as c:
        results = await asyncio_gather(*[_query(c, {"keywords": "nurse"}) for _ in range(50)])
    assert all(r.status_code == 200 for r in results)
    bodies = {r.content for r in results}
    assert len(bodies) == 1  # every concurrent caller got the identical payload
    assert counting.calls == 1  # 50 identical concurrent misses -> ONE backend search


async def test_concurrent_mixed_load(index):
    # 200 concurrent requests across 5 distinct queries: all succeed with correct, stable results,
    # and single-flight+cache bound the DB work to ~one search per distinct query (not 200).
    svc = SearchService(index, max_db_threads=4)
    counting = _CountingBackend(SqliteIndexBackend(index), delay=0.02)
    svc.backend = counting  # type: ignore[assignment]
    app = QueryApp(svc)
    queries = [{"keywords": k} for k in ("nurse", "engineer", "analyst", "lab", "research")]
    async with _client(app) as c:
        reqs = [_query(c, queries[i % len(queries)]) for i in range(200)]
        results = await asyncio_gather(*reqs)
    assert all(r.status_code == 200 for r in results)
    # results for the same query are byte-identical regardless of concurrency
    by_kw: dict[str, set[bytes]] = {}
    for i, r in enumerate(results):
        by_kw.setdefault(queries[i % len(queries)]["keywords"], set()).add(r.content)
    assert all(len(v) == 1 for v in by_kw.values())
    # 200 concurrent requests over 5 distinct queries -> <=5 real searches (coalesced + cached)
    assert counting.calls <= len(queries)


async def asyncio_gather(*aws):
    """Run awaitables concurrently under anyio (portable across the anyio backends)."""
    results: list = [None] * len(aws)

    async def run(i, aw):
        results[i] = await aw

    async with anyio.create_task_group() as tg:
        for i, aw in enumerate(aws):
            tg.start_soon(run, i, aw)
    return results
