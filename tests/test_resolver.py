"""Unit tests for the ATS auto-discovery resolver (offline + respx-mocked network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobspine.http import AsyncFetcher
from jobspine.registry.resolver import Resolution, aresolve, resolve

pytestmark = pytest.mark.anyio


# --- Tier 1: provider pattern matching (offline) ---------------------------


def test_resolve_greenhouse_url() -> None:
    res = resolve("boards.greenhouse.io/stripe")
    assert res.matched is True
    assert bool(res) is True
    assert res.ats == "greenhouse"
    assert res.token == "stripe"


def test_resolve_greenhouse_full_url() -> None:
    res = resolve("https://job-boards.greenhouse.io/airbnb?gh_jid=1#x")
    assert res.ats == "greenhouse"
    assert res.token == "airbnb"


def test_resolve_lever_url() -> None:
    res = resolve("jobs.lever.co/spotify")
    assert res.ats == "lever"
    assert res.token == "spotify"


def test_resolve_ashby_url() -> None:
    res = resolve("https://jobs.ashbyhq.com/ramp")
    assert res.ats == "ashby"
    assert res.token == "ramp"


def test_resolve_workday_url() -> None:
    res = resolve("nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x")
    assert res.ats == "workday"
    assert res.token == "nvidia|wd5|NVIDIAExternalCareerSite"


# --- Tier 2: seed registry lookup (offline) --------------------------------


def test_resolve_via_seed_domain() -> None:
    res = resolve("stripe.com")
    assert res.matched is True
    assert res.ats == "greenhouse"
    assert res.token == "stripe"
    assert res.domain == "stripe.com"
    assert res.source == "stripe.com"


def test_resolve_via_seed_full_careers_url() -> None:
    res = resolve("https://www.openai.com/careers")
    assert res.ats == "ashby"
    assert res.token == "openai"


# --- Unmatched -------------------------------------------------------------


def test_resolve_unknown_is_unmatched() -> None:
    res = resolve("unknown.example")
    assert res.matched is False
    assert bool(res) is False
    assert res.ats is None
    assert res.token is None
    assert res.source == "unknown.example"


def test_resolve_never_raises_on_garbage() -> None:
    assert resolve("").matched is False
    assert resolve("::::not a url::::").matched is False


def test_resolution_bool_protocol() -> None:
    assert bool(Resolution(matched=True))
    assert not bool(Resolution(matched=False))


# --- Tier 3: aresolve embedded-signature discovery (respx-mocked) ----------


async def test_aresolve_returns_sync_hit_without_network() -> None:
    with respx.mock:
        async with AsyncFetcher(per_host_rate=100) as f:
            res = await aresolve("jobs.lever.co/palantir", f)
    assert res.ats == "lever"
    assert res.token == "palantir"


async def test_aresolve_detects_embedded_greenhouse() -> None:
    careers_html = """
    <html><body>
      <h1>Join Acme</h1>
      <iframe src="https://boards.greenhouse.io/acmeinc"></iframe>
    </body></html>
    """
    with respx.mock:
        careers = respx.get("https://acme.example/careers").mock(
            return_value=httpx.Response(200, html=careers_html)
        )
        gh = respx.get("https://boards.greenhouse.io/acmeinc").mock(
            return_value=httpx.Response(200, html="<html>live board</html>")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            res = await aresolve("https://acme.example/careers", f)

    assert careers.called
    assert gh.called
    assert res.matched is True
    assert res.ats == "greenhouse"
    assert res.token == "acmeinc"
    assert res.source == "https://acme.example/careers"


async def test_aresolve_probes_multiple_candidates_concurrently() -> None:
    # Two embedded ATS signatures -> two probes; both must be launched (concurrently) and
    # the first signature in the document (greenhouse) wins.
    careers_html = """
    <html><body>
      <a href="https://boards.greenhouse.io/multicorp">GH board</a>
      <iframe src="https://jobs.lever.co/multicorp"></iframe>
    </body></html>
    """
    with respx.mock:
        careers = respx.get("https://multicorp.example/jobs").mock(
            return_value=httpx.Response(200, html=careers_html)
        )
        gh = respx.get("https://boards.greenhouse.io/multicorp").mock(
            return_value=httpx.Response(200, html="ok")
        )
        lever = respx.get("https://jobs.lever.co/multicorp").mock(
            return_value=httpx.Response(200, html="ok")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            res = await aresolve("https://multicorp.example/jobs", f)

    assert careers.called
    # Both candidate endpoints were probed -> concurrent fan-out actually happened.
    assert gh.called
    assert lever.called
    # Earliest-in-document signature wins deterministically.
    assert res.ats == "greenhouse"
    assert res.token == "multicorp"


async def test_aresolve_unmatched_when_no_signatures() -> None:
    with respx.mock:
        respx.get("https://plain.example/careers").mock(
            return_value=httpx.Response(200, html="<html>no ats here</html>")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            res = await aresolve("https://plain.example/careers", f)
    assert res.matched is False


async def test_aresolve_unmatched_when_page_fetch_fails() -> None:
    with respx.mock:
        respx.get("https://down.example/careers").mock(return_value=httpx.Response(500))
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            res = await aresolve("https://down.example/careers", f)
    assert res.matched is False
