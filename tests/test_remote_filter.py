"""remote=True is a PRECISE filter (remote/hybrid or a remote signal in the location), with the
index SQL and SearchQuery.matches() in agreement. Untagged onsite postings are dropped."""

from __future__ import annotations

from ergon.index.backend import SqliteIndexBackend
from ergon.index.build import build_index
from ergon.models import JobPosting, Location, RemoteType, SearchQuery


def _job(sid, *, title="Engineer", remote=RemoteType.UNKNOWN, raw="", is_remote=False):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company="Co",
        title=title,
        remote=remote,
        locations=[Location(raw=raw, is_remote=is_remote)],
    )


def test_matches_remote_is_precise():
    q = SearchQuery(remote=True)
    assert q.matches(_job("1", remote=RemoteType.REMOTE))
    assert q.matches(_job("2", remote=RemoteType.HYBRID))
    assert q.matches(_job("3", raw="Remote", is_remote=True))
    assert q.matches(_job("4", remote=RemoteType.UNKNOWN, raw="Remote - US"))  # text signal
    # untagged onsite with no remote signal -> dropped (was kept under the old lenient rule)
    assert not q.matches(_job("5", remote=RemoteType.UNKNOWN, raw="New York, NY"))
    assert not q.matches(_job("6", remote=RemoteType.ONSITE, raw="Austin, TX"))


def test_index_remote_matches_sdk(tmp_path):
    # distinct titles so fuzzy dedup keeps all five
    jobs = [
        _job("1", title="Backend Engineer", remote=RemoteType.REMOTE, raw="Remote"),
        _job("2", title="Frontend Engineer", remote=RemoteType.HYBRID, raw="NYC (Hybrid)"),
        _job("3", title="Data Engineer", remote=RemoteType.UNKNOWN, raw="Remote - US"),  # text-only
        _job(
            "4", title="ML Engineer", remote=RemoteType.UNKNOWN, raw="New York, NY"
        ),  # onsite-excl
        _job("5", title="Platform Engineer", remote=RemoteType.ONSITE, raw="Austin, TX"),  # excl
    ]
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    q = SearchQuery(remote=True, limit=50)
    index_hits = {j.id for j in SqliteIndexBackend(p).search(q)}
    sdk_hits = {j.id for j in jobs if q.matches(j)}
    assert index_hits == sdk_hits  # parity
    assert len(index_hits) == 3  # jobs 1,2,3 only
