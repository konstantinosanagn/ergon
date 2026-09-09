"""Tests for the career-page ATS resolver (pure extraction + URL building)."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.anyio

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "_resolve_careers", ROOT / "scripts" / "resolve_careers.py"
)
rc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rc)  # type: ignore[union-attr]

from ergon.providers.base import load_builtins  # noqa: E402

load_builtins()


def test_extracts_workday_from_careers_html():
    html = """
    <html><body><a href="https://about.us/contact">Contact</a>
    <a href="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite">View jobs</a>
    </body></html>
    """
    links = rc.extract_ats_links(html)
    assert ("workday", "nvidia|wd5|NVIDIAExternalCareerSite") in links


def test_extracts_from_redirect_final_url():
    # careers.x.com 302 -> the ATS itself; the final URL alone must resolve.
    links = rc.extract_ats_links(
        "<html>no links</html>", final_url="https://boards.greenhouse.io/airbnb"
    )
    assert links == [("greenhouse", "airbnb")]


def test_prefers_real_ats_over_fallback_and_dedups():
    html = """
    <a href="https://boards.greenhouse.io/acme">jobs</a>
    <a href="https://boards.greenhouse.io/acme">jobs again</a>
    """
    links = rc.extract_ats_links(html)
    assert links == [("greenhouse", "acme")]  # deduped


def test_shared_cdn_token_is_filtered():
    # A careers page embeds the vendor CDN (cdn.phenompeople.com) which matches() greedily claims;
    # it is NOT the company's board and must be dropped (else a false candidate).
    html = '<script src="https://cdn.phenompeople.com/foo.js"></script>'
    assert rc.extract_ats_links(html) == []


def test_non_ats_page_yields_nothing():
    html = '<a href="https://example.com/about">About</a><a href="https://twitter.com/x">x</a>'
    assert rc.extract_ats_links(html) == []


def test_guess_domains_uses_brand_tokens():
    doms = rc.guess_domains("NVIDIA Corporation")
    assert "nvidia.com" in doms
    doms2 = rc.guess_domains("Palantir Technologies Inc")
    assert "palantir.com" in doms2  # 'technologies' is a generic descriptor -> dropped


def test_careers_urls_shape():
    urls = rc.careers_urls("nvidia.com")
    assert "https://nvidia.com/careers" in urls
    assert "https://careers.nvidia.com" in urls


def test_plausibility_guard_rejects_wrong_company_domain():
    # Clearbit returns mdimembrane.com for "Advanced Micro Devices" — must NOT attribute to AMD.
    assert not rc._plausible("Advanced Micro Devices", "mdimembrane.com", "mdimembrane")
    # Token-EXACT, not substring: "stripe" is a substring of "stripersonline" but must be rejected.
    assert not rc._plausible("Stripe", "stripersonline.com", "x")
    # Correct domains are accepted (including multi-token joins).
    assert rc._plausible("Salesforce", "salesforce.com", "salesforce|wd12|External_Career_Site")
    assert rc._plausible("Exxon Mobil", "exxonmobil.com", "exxonmobil")
    assert rc._plausible("Airbnb", "airbnb.com", "airbnb")


def test_same_origin_js_only_returns_same_host_bundles():
    html = """
    <script src="/static/main.abc.js"></script>
    <script src="https://careers.acme.com/app.js"></script>
    <script src="https://cdn.thirdparty.com/vendor.js"></script>
    """
    js = rc._same_origin_js(html, "https://careers.acme.com/careers")
    assert "https://careers.acme.com/static/main.abc.js" in js
    assert "https://careers.acme.com/app.js" in js
    assert all("cdn.thirdparty.com" not in u for u in js)  # cross-origin excluded


async def test_company_domains_uses_clearbit_then_guess():
    import httpx
    import respx

    from ergon.http import AsyncFetcher

    payload = [{"name": "Salesforce", "domain": "salesforce.com"}]
    with respx.mock:
        respx.get("https://autocomplete.clearbit.com/v1/companies/suggest").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            doms = await rc.company_domains("Salesforce", f)
    assert doms[0] == "salesforce.com"  # Clearbit first
    assert "salesforce.com" in doms


async def test_company_domains_override_short_circuits():
    from ergon.http import AsyncFetcher

    async with AsyncFetcher() as f:
        assert await rc.company_domains("Anything", f, override="given.com") == ["given.com"]
