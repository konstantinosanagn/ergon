"""Tests for the sync facade's staleness-guard defaulting (index-freshness fix).

Both the module-level ``search()`` and ``ErgonTracker.search()`` default
``max_last_seen_age_days`` to 21 unless ``include_stale=True`` is passed, or the caller already
supplied an explicit ``max_last_seen_age_days=`` (preserved via setdefault semantics).
"""

from __future__ import annotations

from ergon_tracker.models import SearchResult
from ergon_tracker.sync import ErgonTracker, search


def _capture_query(monkeypatch):
    captured = {}

    async def fake_run_search(q, fetcher):
        captured["query"] = q
        return SearchResult(jobs=[], health=[])

    monkeypatch.setattr("ergon_tracker.engine.run_search", fake_run_search)
    return captured


def test_module_search_defaults_max_last_seen_age_days_21(monkeypatch):
    captured = _capture_query(monkeypatch)
    search("engineer")
    assert captured["query"].max_last_seen_age_days == 21


def test_module_search_include_stale_leaves_it_none(monkeypatch):
    captured = _capture_query(monkeypatch)
    search("engineer", include_stale=True)
    assert captured["query"].max_last_seen_age_days is None


def test_module_search_explicit_value_preserved(monkeypatch):
    captured = _capture_query(monkeypatch)
    search("engineer", max_last_seen_age_days=7)
    assert captured["query"].max_last_seen_age_days == 7


def test_module_search_explicit_value_preserved_even_with_include_stale(monkeypatch):
    captured = _capture_query(monkeypatch)
    search("engineer", include_stale=True, max_last_seen_age_days=7)
    assert captured["query"].max_last_seen_age_days == 7


def test_ergon_tracker_search_defaults_max_last_seen_age_days_21(monkeypatch):
    captured = _capture_query(monkeypatch)
    ErgonTracker().search("engineer")
    assert captured["query"].max_last_seen_age_days == 21


def test_ergon_tracker_search_include_stale_leaves_it_none(monkeypatch):
    captured = _capture_query(monkeypatch)
    ErgonTracker().search("engineer", include_stale=True)
    assert captured["query"].max_last_seen_age_days is None


def test_ergon_tracker_search_explicit_value_preserved(monkeypatch):
    captured = _capture_query(monkeypatch)
    ErgonTracker().search("engineer", max_last_seen_age_days=7)
    assert captured["query"].max_last_seen_age_days == 7
