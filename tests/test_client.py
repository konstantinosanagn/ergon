"""Tests for AsyncErgonTracker.search's staleness-guard defaulting (index-freshness fix).

By default, a query that doesn't already set ``max_last_seen_age_days`` gets it defaulted to 21
(the safe staleness backstop — see SearchQuery.max_last_seen_age_days). ``include_stale=True``
opts out of that defaulting; an explicit value already on the query is always preserved.
"""

from __future__ import annotations

import pytest

from ergon_tracker.client import AsyncErgonTracker
from ergon_tracker.models import SearchQuery, SearchResult

pytestmark = pytest.mark.anyio


async def _search_and_capture(monkeypatch, query, **kwargs):
    captured = {}

    async def fake_run_search(q, fetcher):
        captured["query"] = q
        return SearchResult(jobs=[], health=[])

    monkeypatch.setattr("ergon_tracker.engine.run_search", fake_run_search)
    tracker = AsyncErgonTracker()
    await tracker.search(query, **kwargs)
    return captured["query"]


async def test_default_search_sets_max_last_seen_age_days_21(monkeypatch):
    q = await _search_and_capture(monkeypatch, SearchQuery(keywords="engineer"))
    assert q.max_last_seen_age_days == 21


async def test_include_stale_leaves_it_none(monkeypatch):
    q = await _search_and_capture(monkeypatch, SearchQuery(keywords="engineer"), include_stale=True)
    assert q.max_last_seen_age_days is None


async def test_explicit_max_last_seen_age_days_is_preserved(monkeypatch):
    # Caller already set an explicit guard on the query -> not overridden to 21.
    q = await _search_and_capture(
        monkeypatch, SearchQuery(keywords="engineer", max_last_seen_age_days=7)
    )
    assert q.max_last_seen_age_days == 7


async def test_explicit_value_preserved_even_with_include_stale_false(monkeypatch):
    q = await _search_and_capture(
        monkeypatch,
        SearchQuery(keywords="engineer", max_last_seen_age_days=7),
        include_stale=False,
    )
    assert q.max_last_seen_age_days == 7
