"""Tests for the career-page ATS resolver (pure extraction + URL building)."""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "_resolve_careers", ROOT / "scripts" / "resolve_careers.py"
)
rc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rc)  # type: ignore[union-attr]

from ergon_tracker.providers.base import load_builtins  # noqa: E402

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
