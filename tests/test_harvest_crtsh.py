"""Tests for the crt.sh registry harvester's pure parsing/extraction logic.

These cover the no-network functions: crt.sh payload flattening, tenant extraction per ATS,
reserved/infra filtering, and Workday career-site discovery from page HTML.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_crtsh import (  # noqa: E402
    CONFIGS,
    extract_tenants,
    extract_workday_site,
    parse_crtsh_hosts,
)


def test_parse_crtsh_hosts_flattens_dedupes_and_strips_wildcards() -> None:
    payload = json.dumps(
        [
            {"name_value": "acme.recruitee.com\n*.recruitee.com"},
            {"name_value": "ACME.recruitee.com"},  # case-insensitive dedupe
            {"name_value": "globex.recruitee.com"},
            {"common_name": "fallback.recruitee.com"},  # common_name fallback
        ]
    )
    hosts = parse_crtsh_hosts(payload)
    assert hosts == [
        "acme.recruitee.com",
        "fallback.recruitee.com",
        "globex.recruitee.com",
        "recruitee.com",  # the "*." wildcard collapses to the bare apex
    ]


def test_parse_crtsh_hosts_bad_payload_returns_empty() -> None:
    assert parse_crtsh_hosts("not json") == []
    assert parse_crtsh_hosts(json.dumps({"not": "a list"})) == []


def test_extract_tenants_recruitee_filters_infra_and_multilevel() -> None:
    hosts = [
        "acme.recruitee.com",  # keep
        "globex.recruitee.com",  # keep
        "api.recruitee.com",  # drop: reserved infra
        "www.recruitee.com",  # drop: reserved infra
        "recruitee.com",  # drop: apex, no tenant
        "foo.bar.recruitee.com",  # drop: multi-level (not a single tenant label)
        "acme.recruitee.com",  # dup
    ]
    tenants = extract_tenants(CONFIGS["recruitee"], hosts)
    assert tenants == [{"tenant": "acme"}, {"tenant": "globex"}]


def test_extract_tenants_personio_matches_de_and_com() -> None:
    hosts = ["acme.jobs.personio.de", "globex.jobs.personio.com", "noise.example.com"]
    tenants = extract_tenants(CONFIGS["personio"], hosts)
    assert {t["tenant"] for t in tenants} == {"acme", "globex"}


def test_extract_tenants_workday_captures_datacenter() -> None:
    hosts = [
        "nvidia.wd5.myworkdayjobs.com",
        "salesforce.wd1.myworkdayjobs.com",
        "impl.wd12.myworkdayjobs.com",
        "www.myworkdayjobs.com",  # drop: reserved + no wd
    ]
    tenants = extract_tenants(CONFIGS["workday"], hosts)
    assert {"tenant": "nvidia", "wd": "wd5"} in tenants
    assert {"tenant": "salesforce", "wd": "wd1"} in tenants
    assert {"tenant": "impl", "wd": "wd12"} in tenants
    assert len(tenants) == 3


def test_extract_tenants_rejects_all_numeric_labels() -> None:
    # Numeric-only subdomains (e.g. shard ids) are never company tenants.
    tenants = extract_tenants(CONFIGS["recruitee"], ["12345.recruitee.com", "acme.recruitee.com"])
    assert tenants == [{"tenant": "acme"}]


def test_extract_workday_site_reads_cxs_segment() -> None:
    html = (
        '<script>fetch("/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs")</script>'
        '<a href="/en-US/NVIDIAExternalCareerSite/job/123">role</a>'
    )
    assert extract_workday_site(html, "nvidia") == "NVIDIAExternalCareerSite"


def test_extract_workday_site_absent_returns_none() -> None:
    assert extract_workday_site("<html>no cxs reference here</html>", "nvidia") is None
