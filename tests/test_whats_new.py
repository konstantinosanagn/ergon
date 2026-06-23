"""Tests for the 'what's new' feed: whats_new_rows (index recency filter) + the MCP tool wiring."""

from __future__ import annotations

from ergon_tracker.index.build import build_index
from ergon_tracker.index.db import connect
from ergon_tracker.index.query import whats_new_rows
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType, SearchQuery


def _job(sid, title, **kw):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=kw.pop("company", "Co"),
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def _build(tmp_path, jobs):
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    return p


def test_filters_by_first_seen(tmp_path):
    p = _build(tmp_path, [_job("1", "New Role"), _job("2", "Old Role"), _job("3", "Ancient Role")])
    con = connect(p)
    con.execute("UPDATE jobs SET first_seen='2026-06-20' WHERE title='New Role'")
    con.execute("UPDATE jobs SET first_seen='2026-06-10' WHERE title='Old Role'")
    con.execute("UPDATE jobs SET first_seen='2026-01-01' WHERE title='Ancient Role'")
    con.commit()
    titles = {r["title"] for r in whats_new_rows(con, SearchQuery(limit=50), "2026-06-15")}
    assert titles == {"New Role"}  # only first_seen >= cutoff


def test_newest_first_ordering(tmp_path):
    p = _build(tmp_path, [_job("1", "A Role"), _job("2", "B Role")])
    con = connect(p)
    con.execute("UPDATE jobs SET first_seen='2026-06-18' WHERE title='A Role'")
    con.execute("UPDATE jobs SET first_seen='2026-06-21' WHERE title='B Role'")
    con.commit()
    rows = whats_new_rows(con, SearchQuery(limit=50), "2026-06-01")
    assert [r["title"] for r in rows] == ["B Role", "A Role"]  # newest first_seen first


def test_include_changed_picks_up_updated_old_jobs(tmp_path):
    p = _build(tmp_path, [_job("1", "Updated Old Role"), _job("2", "Stale Old Role")])
    con = connect(p)
    # both first seen long ago; one was updated inside the window
    con.execute("UPDATE jobs SET first_seen='2026-01-01', updated_at=NULL")
    con.execute("UPDATE jobs SET updated_at='2026-06-21' WHERE title='Updated Old Role'")
    con.commit()
    q = SearchQuery(limit=50)
    assert whats_new_rows(con, q, "2026-06-15") == []  # nothing NEW
    changed = {r["title"] for r in whats_new_rows(con, q, "2026-06-15", include_changed=True)}
    assert changed == {"Updated Old Role"}  # the updated one surfaces


def test_filters_compose_with_recency(tmp_path):
    p = _build(
        tmp_path,
        [
            _job("1", "Senior Engineer", description_text="5+ years required"),
            _job("2", "Junior Analyst"),
        ],
    )
    con = connect(p)
    con.execute("UPDATE jobs SET first_seen='2026-06-20'")
    con.execute("UPDATE jobs SET level='senior' WHERE title='Senior Engineer'")
    con.execute("UPDATE jobs SET level='junior' WHERE title='Junior Analyst'")
    con.commit()
    rows = whats_new_rows(con, SearchQuery(level=JobLevel.SENIOR, limit=50), "2026-06-15")
    assert [r["title"] for r in rows] == ["Senior Engineer"]


def test_mcp_tool_is_registered():
    # the tool must be discoverable by an MCP client
    import anyio

    from ergon_tracker import mcp_server

    names = anyio.run(lambda: _tool_names(mcp_server.mcp))
    assert "whats_new" in names


async def _tool_names(mcp):
    return {t.name for t in await mcp.list_tools()}


def test_mcp_tool_happy_path_end_to_end(tmp_path, monkeypatch):
    from datetime import date, timedelta

    p = _build(
        tmp_path,
        [
            _job("1", "New Engineer", description_text="build"),
            _job("2", "Old Engineer", description_text="build"),
        ],
    )
    con = connect(p)
    # date-relative so the test never ages out
    con.execute(
        "UPDATE jobs SET first_seen=? WHERE title='New Engineer'", (date.today().isoformat(),)
    )
    con.execute(
        "UPDATE jobs SET first_seen=? WHERE title='Old Engineer'",
        ((date.today() - timedelta(days=365)).isoformat(),),
    )
    con.commit()
    con.close()

    from ergon_tracker.index import cache as cache_mod

    monkeypatch.setattr(cache_mod.IndexCache, "ensure_fresh", lambda self: p)
    monkeypatch.setattr(cache_mod, "cached_index_build_id", lambda *a, **k: "b1")

    from ergon_tracker import mcp_server

    res = mcp_server.whats_new(since_days=30, keywords="engineer", limit=10)
    assert res["count"] == 1
    job = res["jobs"][0]
    assert job["title"] == "New Engineer" and job["is_new"] is True
    assert job["first_seen"] == date.today().isoformat()
    assert res["as_of"] == "b1"


def test_mcp_tool_index_unavailable_is_graceful(monkeypatch):
    from ergon_tracker.index import cache as cache_mod

    monkeypatch.setattr(
        cache_mod.IndexCache,
        "ensure_fresh",
        lambda self: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    from ergon_tracker import mcp_server

    res = mcp_server.whats_new(since_days=7)
    assert res["count"] == 0 and "unavailable" in res["note"]
