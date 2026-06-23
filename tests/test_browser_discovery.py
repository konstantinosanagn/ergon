"""Unit tests for the browser-discovery spec proposer (pure response-shape classification).

Fixtures mirror real captured shapes (amazon.jobs, jobs.apple.com, akkodis, WordPress) so the
proposer is regression-tested against the actual apicapture specs it must reproduce."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from browser_discovery import (  # noqa: E402
    find_records_path,
    map_fields,
    propose_spec,
)


def test_amazon_style_get_offset():
    req = {
        "url": "https://www.amazon.jobs/en/search.json?result_limit=100&offset=0",
        "method": "GET",
    }
    resp = {
        "jobs": [
            {
                "id": "1",
                "title": "SDE",
                "normalized_location": "Seattle",
                "job_path": "/x",
                "business_category": "Eng",
            }
        ],
        "hits": 342,
    }
    spec = propose_spec(req, resp, company="Amazon", token="amazon")
    assert spec["records_path"] == ["jobs"]
    assert spec["total_path"] == ["hits"]
    assert spec["fields"]["title"] == "title" and spec["fields"]["id"] == "id"
    assert spec["fields"]["location"] == "normalized_location"
    assert spec["fields"]["url"] == "job_path"
    assert spec["page_param"] == "offset" and spec["page_step"] == 100  # from result_limit


def test_apple_style_post_nested_records_and_pagination():
    req = {
        "url": "https://jobs.apple.com/api/v1/search",
        "method": "POST",
        "body": {"query": "ml", "page": 1},
    }
    resp = {
        "res": {
            "searchResults": [
                {
                    "positionId": "200",
                    "postingTitle": "ML Eng",
                    "locations": ["Cupertino"],
                    "team": {"teamName": "AIML"},
                }
            ],
            "totalRecords": 50,
        }
    }
    spec = propose_spec(req, resp, company="Apple", token="apple")
    assert spec["records_path"] == ["res", "searchResults"]
    assert spec["total_path"] == ["res", "totalRecords"]
    assert spec["fields"]["title"] == "postingTitle" and spec["fields"]["id"] == "positionId"
    assert spec["fields"]["department"] == "team.teamName"  # parent-key fallback
    assert spec["page_path"] == ["page"] and spec["page_start"] == 1
    assert spec["method"] == "POST" and spec["body"]["page"] == 1


def test_wordpress_rendered_nested_title():
    # WordPress wp-json nests the scalar under .rendered — parent-key fallback must catch it
    resp = [
        {
            "id": 9,
            "title": {"rendered": "Engineer"},
            "link": "https://x.com/9",
            "content": {"rendered": "<p>desc</p>"},
        }
    ]
    fields = map_fields(resp)
    assert fields["title"] == "title.rendered"
    assert fields["id"] == "id" and fields["url"] == "link"
    assert find_records_path(resp) == []  # top-level array


def test_akkodis_style_nested_total():
    resp = {
        "jobs": [{"jobId": "5", "jobTitle": "DevOps", "jobLocation": "NYC"}],
        "facets": {"count": 88},
    }
    spec = propose_spec(
        {"url": "https://x/api", "method": "POST", "body": {}},
        resp,
        company="Akkodis",
        token="akkodis",
    )
    assert spec["records_path"] == ["jobs"]
    assert spec["total_path"] == ["facets", "count"]
    assert spec["fields"]["id"] == "jobId" and spec["fields"]["title"] == "jobTitle"


def test_header_scrubbing_drops_volatile():
    req = {
        "url": "https://x/api",
        "method": "POST",
        "body": {},
        "headers": {
            "Content-Type": "application/json",
            "Cookie": "secret=1",
            "Authorization": "Bearer x",
            "User-Agent": "moz",
        },
    }
    resp = {"jobs": [{"id": "1", "title": "T"}]}
    spec = propose_spec(req, resp, company="X", token="x")
    assert spec["headers"] == {"Content-Type": "application/json"}  # cookie/auth/UA dropped


def test_no_title_field_raises():
    with pytest.raises(ValueError):
        propose_spec(
            {"url": "https://x", "method": "GET"},
            {"items": [{"foo": 1, "bar": 2}]},
            company="X",
            token="x",
        )


def test_longest_array_wins_over_decoy():
    # a short non-job list must not beat the real (longer) job list
    resp = {
        "filters": [{"name": "a"}, {"name": "b"}],
        "results": [{"id": str(i), "title": f"J{i}"} for i in range(20)],
    }
    assert find_records_path(resp) == ["results"]
