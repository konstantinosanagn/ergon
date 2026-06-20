"""Unit tests for the PageUp People provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery, make_job_id
from ergon_tracker.providers.pageup import PageUpProvider

pytestmark = pytest.mark.anyio

RSS_URL = "https://careers.pageuppeople.com/669/cw/en-us/rss"


def _item(jid: str, title: str, loc: str, work: str = "Full Time", cat: str = "Research") -> str:
    # job:description is double-escaped HTML, as PageUp emits it.
    return (
        "<item>"
        f"<guid isPermaLink='true'>https://careers.pageuppeople.com/669/cw/en-us/job/{jid}</guid>"
        f"<link>https://careers.pageuppeople.com/669/cw/en-us/job/{jid}</link>"
        f"<title>{title}</title>"
        f"<description>Short teaser for {title}.</description>"
        "<pubDate>Thu, 18 Jun 2026 20:25:00 Z</pubDate>"
        "<a10:updated>2026-06-18T20:25:00Z</a10:updated>"
        f"<job:category>{cat}</job:category>"
        f"<job:refNo>{jid}</job:refNo>"
        f"<job:location>{loc}</job:location>"
        f"<job:workType>{work}</job:workType>"
        "<job:businessLayer1>College of Engineering</job:businessLayer1>"
        f"<job:applyLink>https://careers.pageuppeople.com/669/cw/en-us/job/{jid}/apply</job:applyLink>"
        "<job:description>&lt;p&gt;&lt;b&gt;Full&lt;/b&gt; description.&lt;/p&gt;</job:description>"
        "</item>"
    )


def _rss(items: list[str]) -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss xmlns:job='http://pageuppeople.com/' xmlns:a10='http://www.w3.org/2005/Atom'>"
        "<channel>" + "".join(items) + "</channel></rss>"
    )


def test_parse_token() -> None:
    assert PageUpProvider._parse_token("669|University of Alabama") == (
        "669", "University of Alabama", "en-us", "cw",
    )
    assert PageUpProvider._parse_token("782") == ("782", None, "en-us", "cw")
    assert PageUpProvider._parse_token("959|PageUp|en|ci") == ("959", "PageUp", "en", "ci")


async def test_fetch_parses_feed_and_normalizes() -> None:
    body = _rss([
        _item("529465", "Cartographer", "Alabama|Tuscaloosa"),
        _item("530001", "Remote Research Fellow", "Remote", work="Part Time"),
    ])
    with respx.mock as respx_mock:
        respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=body))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PageUpProvider().fetch("669|University of Alabama", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"University of Alabama"}
    j0 = PageUpProvider().normalize(raws[0])
    assert j0.id == make_job_id("pageup", "529465")
    assert j0.title == "Cartographer"
    assert j0.locations[0].raw == "Alabama, Tuscaloosa"  # pipe-packed -> comma-joined
    assert j0.department == "College of Engineering"
    assert j0.apply_url.endswith("/job/529465/apply")
    assert j0.employment_type.value == "full_time"
    assert "<b>Full</b>" in (j0.description_html or "")  # double-escaping undone once
    jr = PageUpProvider().normalize(raws[1])
    assert jr.remote.value == "remote"
    assert jr.employment_type.value == "part_time"


async def test_fetch_respects_limit() -> None:
    body = _rss([_item(str(i), f"Role {i}", "Troy, NY") for i in range(40)])
    with respx.mock as respx_mock:
        respx_mock.get(RSS_URL).mock(return_value=httpx.Response(200, text=body))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PageUpProvider().fetch("669|University of Alabama", SearchQuery(limit=5), f)
    assert len(raws) == 5


async def test_fetch_empty_on_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(RSS_URL).mock(return_value=httpx.Response(503))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PageUpProvider().fetch("669|University of Alabama", SearchQuery(), f)
    assert raws == []
