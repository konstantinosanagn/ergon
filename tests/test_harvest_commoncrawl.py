"""Tests for the Common Crawl harvester's pure token extractors (no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import (  # noqa: E402
    CONFIGS,
    extract_tokens,
    latest_crawl_api,
    parse_cc_urls,
    parse_num_pages,
    recent_crawl_apis,
    recent_crawl_ids,
)


def test_recent_crawl_ids_returns_n_newest() -> None:
    info = json.dumps(
        [
            {"id": "CC-MAIN-2026-25", "cdx-api": "https://index.commoncrawl.org/x-index"},
            {"id": "CC-MAIN-2026-21", "cdx-api": "https://index.commoncrawl.org/y-index"},
            {"id": "CC-MAIN-2026-17", "cdx-api": "https://index.commoncrawl.org/z-index"},
        ]
    )
    assert recent_crawl_ids(info, 2) == ["CC-MAIN-2026-25", "CC-MAIN-2026-21"]
    assert recent_crawl_ids("garbage", 5) == []
    assert recent_crawl_ids(info, 0) == []
    # entries without an id are skipped, not crash
    assert recent_crawl_ids(json.dumps([{"cdx-api": "x"}, {"id": "CC-MAIN-2026-25"}]), 5) == [
        "CC-MAIN-2026-25"
    ]


def test_recent_crawl_apis_returns_n_newest() -> None:
    info = json.dumps(
        [
            {
                "id": "CC-MAIN-2026-21",
                "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-21-index",
            },
            {
                "id": "CC-MAIN-2026-13",
                "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-13-index",
            },
            {
                "id": "CC-MAIN-2026-05",
                "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-05-index",
            },
        ]
    )
    apis = recent_crawl_apis(info, 2)
    assert apis == [
        "https://index.commoncrawl.org/CC-MAIN-2026-21-index",
        "https://index.commoncrawl.org/CC-MAIN-2026-13-index",
    ]
    assert recent_crawl_apis("garbage", 5) == []
    assert recent_crawl_apis(info, 0) == []


def _extract(ats: str, url: str) -> str | None:
    return CONFIGS[ats].extract(url)  # type: ignore[operator]


def test_parse_num_pages() -> None:
    assert parse_num_pages(json.dumps({"pages": 7, "pageSize": 5, "blocks": 12})) == 7
    assert parse_num_pages("3") == 3
    assert parse_num_pages("garbage") == 1  # flaky response -> at least one page
    assert parse_num_pages(json.dumps({"no_pages": True})) == 1


def test_new_configs_rippling_pinpoint() -> None:
    # rippling is path-based on ats.rippling.com; pinpoint is a *.pinpointhq.com subdomain.
    assert _extract("rippling", "https://ats.rippling.com/acme-corp/jobs/123") == "acme-corp"
    assert _extract("pinpoint", "https://globex.pinpointhq.com/postings.json") == "globex"
    assert _extract("rippling", "https://example.com/x") is None


def test_greenhouse_path_and_embed_for_param() -> None:
    assert _extract("greenhouse", "https://boards.greenhouse.io/stripe") == "stripe"
    assert _extract("greenhouse", "https://boards.greenhouse.io/stripe/jobs/123") == "stripe"
    # embed form carries the token in the ?for= query param
    assert (
        _extract("greenhouse", "https://boards.greenhouse.io/embed/job_board?for=airbnb")
        == "airbnb"
    )
    # junk path segments are rejected
    assert _extract("greenhouse", "https://boards.greenhouse.io/embed/job_app") is None


def test_path_based_extractors() -> None:
    assert _extract("lever", "https://jobs.lever.co/netflix/abc-123") == "netflix"
    assert _extract("ashby", "https://jobs.ashbyhq.com/openai") == "openai"
    assert _extract("workable", "https://apply.workable.com/acme/j/ABC123/") == "acme"


def test_smartrecruiters_preserves_case() -> None:
    # SmartRecruiters slugs are case-sensitive — must NOT be lowercased.
    assert _extract("smartrecruiters", "https://careers.smartrecruiters.com/Visa1/abc") == "Visa1"


def test_subdomain_extractors() -> None:
    assert _extract("bamboohr", "https://acme.bamboohr.com/careers") == "acme"
    assert _extract("breezy", "https://globex.breezy.hr/p/123") == "globex"
    assert _extract("teamtailor", "https://initech.teamtailor.com/jobs") == "initech"
    # apex / infra subdomains rejected
    assert _extract("bamboohr", "https://www.bamboohr.com/pricing") is None


def test_extract_tokens_dedupes_in_order() -> None:
    urls = [
        "https://jobs.lever.co/alpha",
        "https://jobs.lever.co/alpha/x",  # dup token
        "https://jobs.lever.co/beta",
        "https://jobs.lever.co/embed",  # junk
    ]
    assert extract_tokens(CONFIGS["lever"], urls) == ["alpha", "beta"]


def test_parse_cc_urls_skips_bad_lines() -> None:
    nd = (
        json.dumps({"url": "https://jobs.lever.co/a", "status": "200"})
        + "\n"
        + "not json\n"
        + json.dumps({"status": "404"})
        + "\n"  # no url
        + json.dumps({"url": "https://jobs.lever.co/b"})
        + "\n"
    )
    assert parse_cc_urls(nd) == ["https://jobs.lever.co/a", "https://jobs.lever.co/b"]


def test_latest_crawl_api_picks_cdx() -> None:
    info = json.dumps(
        [
            {
                "id": "CC-MAIN-2025-21",
                "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2025-21-index",
            },
            {
                "id": "CC-MAIN-2025-08",
                "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2025-08-index",
            },
        ]
    )
    assert latest_crawl_api(info) == "https://index.commoncrawl.org/CC-MAIN-2025-21-index"
    assert latest_crawl_api("garbage") is None
