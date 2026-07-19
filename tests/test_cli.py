"""Tests that the CLI's --include-stale flag threads through to the same 21-day staleness default
used by the SDK/MCP (index-freshness fix): `search` goes through sync.search(); `match-resume`
builds its own SearchQuery and passes it to resume.rank_by_resume().
"""

from __future__ import annotations

from typer.testing import CliRunner

from ergon_tracker.cli import app
from ergon_tracker.models import SearchResult

runner = CliRunner()


def test_search_command_defaults_max_last_seen_age_days_21(monkeypatch):
    captured = {}

    def fake_search(keywords, **kwargs):
        captured.update(kwargs)
        return SearchResult(jobs=[], health=[])

    monkeypatch.setattr("ergon_tracker.sync.search", fake_search)
    result = runner.invoke(app, ["search", "engineer"])
    assert result.exit_code == 0, result.output
    assert captured["include_stale"] is False


def test_search_command_include_stale_flag_threads_through(monkeypatch):
    captured = {}

    def fake_search(keywords, **kwargs):
        captured.update(kwargs)
        return SearchResult(jobs=[], health=[])

    monkeypatch.setattr("ergon_tracker.sync.search", fake_search)
    result = runner.invoke(app, ["search", "engineer", "--include-stale"])
    assert result.exit_code == 0, result.output
    assert captured["include_stale"] is True


def test_match_resume_command_defaults_max_last_seen_age_days_21(monkeypatch, tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Senior backend engineer, Python, 5 years.")
    captured = {}

    def fake_rank_by_resume(text, query, limit):
        captured["query"] = query
        return [], "semantic_fit"

    monkeypatch.setattr("ergon_tracker.resume.rank_by_resume", fake_rank_by_resume)
    result = runner.invoke(app, ["match-resume", str(resume)])
    assert result.exit_code == 0, result.output
    assert captured["query"].max_last_seen_age_days == 21


def test_match_resume_command_include_stale_leaves_it_none(monkeypatch, tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_text("Senior backend engineer, Python, 5 years.")
    captured = {}

    def fake_rank_by_resume(text, query, limit):
        captured["query"] = query
        return [], "semantic_fit"

    monkeypatch.setattr("ergon_tracker.resume.rank_by_resume", fake_rank_by_resume)
    result = runner.invoke(app, ["match-resume", str(resume), "--include-stale"])
    assert result.exit_code == 0, result.output
    assert captured["query"].max_last_seen_age_days is None
