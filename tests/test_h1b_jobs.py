"""Tests for the H-1B tool surface: SponsorIndex.profile + the h1b_jobs MCP tool (LCA joined to jobs)."""

from __future__ import annotations

from ergon_tracker import mcp_server
from ergon_tracker.extract.visa import SponsorIndex, h1b_profile
from ergon_tracker.models import JobPosting, Location, RemoteType


def test_sponsor_profile_accessor():
    idx = SponsorIndex({"stripe": {"n": 42, "last": "2026-03-01"}})
    assert idx.profile("Stripe") == {"name": "stripe", "filings": 42, "last_filed": "2026-03-01"}
    assert idx.profile("Nonexistent Co") is None
    assert idx.profile(None) is None


def test_h1b_profile_module_helper():
    # the real bundled index — just assert the shape contract holds for a known large sponsor
    p = h1b_profile("Google")
    assert p is None or {"name", "filings", "last_filed"} <= set(p)


def _job(title, company):
    j = JobPosting.create(
        source="greenhouse",
        source_job_id=f"{company}-{title}",
        company=company,
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
    )
    return j.model_copy(update={"visa_sponsor": True})


class _FakeIdx:
    def __init__(self, profiles):
        self.profiles = profiles

    def profile(self, company):
        return self.profiles.get(company)


_PROFILES = {
    "BigSponsor": {"name": "bigsponsor", "filings": 500, "last_filed": "2026-01-01"},
    "SmallSponsor": {"name": "smallsponsor", "filings": 3, "last_filed": "2019-01-01"},
}


def _patch(monkeypatch, pool):
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: list(pool))
    monkeypatch.setattr(
        "ergon_tracker.extract.visa.load_sponsor_index", lambda: _FakeIdx(_PROFILES)
    )


def test_annotates_and_ranks_by_sponsor_strength(monkeypatch):
    _patch(monkeypatch, [_job("Eng B", "SmallSponsor"), _job("Eng A", "BigSponsor")])
    res = mcp_server.h1b_jobs(limit=10)
    assert res["ranked_by"] == "h1b_sponsor_strength"
    assert [j["company"] for j in res["jobs"]] == ["BigSponsor", "SmallSponsor"]  # filings desc
    big = res["jobs"][0]
    assert (
        big["h1b_filings"] == 500
        and big["h1b_last_filed"] == "2026-01-01"
        and big["h1b_active"] is True
    )
    assert (
        res["jobs"][1]["h1b_filings"] == 3 and res["jobs"][1]["h1b_active"] is False
    )  # 2019 = stale


def test_min_filings_drops_token_sponsors(monkeypatch):
    _patch(monkeypatch, [_job("Eng A", "BigSponsor"), _job("Eng B", "SmallSponsor")])
    res = mcp_server.h1b_jobs(min_filings=100, limit=10)
    assert [j["company"] for j in res["jobs"]] == ["BigSponsor"]


def test_active_within_years_drops_quiet_sponsors(monkeypatch):
    _patch(monkeypatch, [_job("Eng A", "BigSponsor"), _job("Eng B", "SmallSponsor")])
    res = mcp_server.h1b_jobs(
        active_within_years=3, limit=10
    )  # SmallSponsor last filed 2019 -> dropped
    assert [j["company"] for j in res["jobs"]] == ["BigSponsor"]


def test_index_unavailable_is_graceful(monkeypatch):
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: None)
    res = mcp_server.h1b_jobs()
    assert res["count"] == 0 and "index unavailable" in res["note"]


def test_defaults_max_last_seen_age_days_21(monkeypatch):
    # index-freshness fix: h1b_jobs builds its own SearchQuery and never exposed a
    # max_last_seen_age_days param -> it should default the staleness guard to 21, same as
    # search_jobs, for consistency.
    captured = {}

    def fake_try_index(q):
        captured["q"] = q
        return []

    monkeypatch.setattr("ergon_tracker.index.router.try_index", fake_try_index)
    mcp_server.h1b_jobs()
    assert captured["q"].max_last_seen_age_days == 21
