"""Tier-2 token injection + stale-on-failure in the apicapture provider (offline, respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery
from ergon_tracker.providers import apicapture as ap
from ergon_tracker.providers.apicapture import ApiCaptureProvider, apply_token_to_spec
from ergon_tracker.token_store import TokenStore

pytestmark = pytest.mark.anyio


# --- pure injection helper -----------------------------------------------------------------------
def test_apply_token_header_does_not_mutate_original():
    spec = {"url": "https://x/api", "method": "POST", "token_inject": {"header": "x-tok"}}
    out = apply_token_to_spec(spec, "ABC")
    assert out["headers"]["x-tok"] == "ABC"
    assert "headers" not in spec  # original untouched -> no stale-token leak across calls


def test_apply_token_body_cookie_query():
    spec = {
        "url": "https://x/api?a=1",
        "method": "POST",
        "body": {"q": "x"},
        "token_inject": {"body_path": ["session"], "cookie": "bm_sv", "query": "t"},
    }
    out = apply_token_to_spec(spec, "Z")
    assert out["body"]["session"] == "Z"
    assert out["headers"]["Cookie"] == "bm_sv=Z"
    assert "t=Z" in out["url"]


def _spec(token_inject):
    return {
        "acme": {
            "company": "Acme",
            "url": "https://acme.test/api/jobs",
            "method": "GET",
            "records_path": ["jobs"],
            "fields": {"id": "id", "title": "title"},
            "token_ref": "acme",
            "token_inject": token_inject,
        }
    }


# --- integration: cached token is injected into the live request ---------------------------------
async def test_token_injected_into_request(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "t.json")
    store.set("acme", "SECRET", ttl_seconds=600)
    monkeypatch.setattr(ap, "_token_store", lambda: store)
    monkeypatch.setattr(ap, "_load_specs", lambda: _spec({"header": "x-myjobstoken"}))
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"jobs": [{"id": "1", "title": "Engineer"}]})

    with respx.mock:
        respx.get("https://acme.test/api/jobs").mock(side_effect=handler)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ApiCaptureProvider().fetch("acme", SearchQuery(limit=5), f)
    assert seen.get("x-myjobstoken") == "SECRET"
    assert len(raws) == 1 and raws[0].source_job_id == "1"


# --- integration: 403 with a cached token -> [] AND the token is marked stale for re-mint ---------
async def test_403_marks_token_stale(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "t.json")
    store.set("acme", "OLD", ttl_seconds=600)
    monkeypatch.setattr(ap, "_token_store", lambda: store)
    monkeypatch.setattr(ap, "_load_specs", lambda: _spec({"header": "x-tok"}))
    with respx.mock:
        respx.get("https://acme.test/api/jobs").mock(
            return_value=httpx.Response(403, text="denied")
        )
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            raws = await ApiCaptureProvider().fetch("acme", SearchQuery(), f)
    assert raws == []
    assert store.get("acme") is None  # marked stale -> the offline cron re-mints next run


# --- regression: a spec WITHOUT token_ref never consults the store -------------------------------
async def test_non_token_spec_ignores_store(tmp_path, monkeypatch):
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("store must not be consulted for a non-token spec")

    monkeypatch.setattr(ap, "_token_store", boom)
    monkeypatch.setattr(
        ap,
        "_load_specs",
        lambda: {
            "acme": {
                "company": "Acme",
                "url": "https://acme.test/api/jobs",
                "method": "GET",
                "records_path": ["jobs"],
                "fields": {"id": "id", "title": "title"},
            }
        },
    )
    with respx.mock:
        respx.get("https://acme.test/api/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": [{"id": "9", "title": "X"}]})
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ApiCaptureProvider().fetch("acme", SearchQuery(), f)
    assert len(raws) == 1 and called["n"] == 0
