"""Tests for the DuckDB CC-columnar harvester's pure helpers (no network, no duckdb)."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_ccduck import (  # noqa: E402
    DEFAULT_CRAWL,
    resolve_default_crawls,
    surt_bounds,
    warc_parquet_urls,
)
from harvest_commoncrawl import CONFIGS  # noqa: E402


def test_resolve_default_crawls_uses_latest_n_from_collinfo() -> None:
    info = json.dumps(
        [
            {"id": "CC-MAIN-2026-25", "cdx-api": "x"},
            {"id": "CC-MAIN-2026-21", "cdx-api": "y"},
            {"id": "CC-MAIN-2026-17", "cdx-api": "z"},
            {"id": "CC-MAIN-2026-12", "cdx-api": "w"},
        ]
    )
    got = resolve_default_crawls(3, fetch=lambda: info)
    assert got == ["CC-MAIN-2026-25", "CC-MAIN-2026-21", "CC-MAIN-2026-17"]


def test_resolve_default_crawls_falls_back_on_network_error() -> None:
    def boom() -> str:
        raise OSError("collinfo unreachable")

    assert resolve_default_crawls(3, fetch=boom) == [DEFAULT_CRAWL]


def test_resolve_default_crawls_falls_back_on_empty() -> None:
    assert resolve_default_crawls(3, fetch=lambda: "[]") == [DEFAULT_CRAWL]


def test_surt_bounds_host_match() -> None:
    lo, hi, col, val = surt_bounds(CONFIGS["greenhouse"])
    assert lo == "io,greenhouse,boards)/"
    assert hi == "io,greenhouse,boards)0"  # '/'(0x2f) -> '0'(0x30)
    assert col == "url_host_name"
    assert val == "boards.greenhouse.io"
    # range is half-open and actually brackets a real key
    assert lo <= "io,greenhouse,boards)/stripe/jobs" < hi


def test_surt_bounds_domain_match() -> None:
    lo, hi, col, val = surt_bounds(CONFIGS["bamboohr"])
    assert lo == "com,bamboohr,"
    assert hi == "com,bamboohr-"  # ','(0x2c) -> '-'(0x2d)
    assert col == "url_host_registered_domain"
    assert val == "bamboohr.com"
    # a subdomain tenant's surtkey falls inside the range
    assert lo <= "com,bamboohr,acme)/careers" < hi


def test_warc_parquet_urls_filters_subset_and_prefixes_https() -> None:
    paths = "\n".join(
        [
            "cc-index/table/cc-main/warc/crawl=CC-MAIN-2024-51/subset=warc/part-0.parquet",
            "cc-index/table/cc-main/warc/crawl=CC-MAIN-2024-51/subset=crawldiagnostics/part-0.parquet",
            "cc-index/table/cc-main/warc/crawl=CC-MAIN-2024-51/subset=robotstxt/part-0.parquet",
            "cc-index/table/cc-main/warc/crawl=CC-MAIN-2024-51/subset=warc/part-1.parquet",
        ]
    )
    urls = warc_parquet_urls(gzip.compress(paths.encode()))
    assert urls == [
        "https://data.commoncrawl.org/cc-index/table/cc-main/warc/crawl=CC-MAIN-2024-51/subset=warc/part-0.parquet",
        "https://data.commoncrawl.org/cc-index/table/cc-main/warc/crawl=CC-MAIN-2024-51/subset=warc/part-1.parquet",
    ]
