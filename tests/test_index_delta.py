"""Row-level deltas (v2.1): a returning user downloads only changed/deleted rows, not the whole file.

build_delta(prev, curr) emits a small file of upserts + deletes; apply_delta(base, delta) mutates a
copy of the cached prev index so it becomes query-equivalent to curr (same ids, content, companies,
FTS) — proven against build_index as the oracle.
"""

from __future__ import annotations

from ergon.index.build import apply_delta, build_delta, build_index
from ergon.index.db import connect
from ergon.models import JobLevel, JobPosting, Location, RemoteType


def _job(sid, company, title, **kw):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=company,
        title=title,
        description_text=kw.pop("desc", "a job description here"),
        locations=[Location(raw=kw.pop("loc", "Remote"), is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def _snapshot(path):
    con = connect(path, read_only=True)
    try:
        jobs = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, content_hash, title FROM jobs")}
        companies = {
            r[0]: r[1] for r in con.execute("SELECT company_key, open_roles FROM companies")
        }
        build_id = con.execute("SELECT value FROM meta WHERE key='build_id'").fetchone()[0]
        return jobs, companies, build_id
    finally:
        con.close()


def test_build_delta_counts_upserts_and_deletes(tmp_path):
    prev = tmp_path / "prev.sqlite"
    curr = tmp_path / "curr.sqlite"
    build_index(
        [
            _job("1", "Stripe", "Backend Engineer", level=JobLevel.SENIOR),
            _job("2", "Ramp", "Frontend Engineer"),
            _job("3", "OpenAI", "ML Engineer"),
        ],
        prev,
        build_id="b0",
    )
    # curr: job 1 unchanged, job 2 changed (new title), job 3 deleted, job 4 new
    build_index(
        [
            _job("1", "Stripe", "Backend Engineer", level=JobLevel.SENIOR),
            _job("2", "Ramp", "Senior Frontend Engineer"),
            _job("4", "NewCo", "Founding Engineer"),
        ],
        curr,
        build_id="b1",
    )
    delta = tmp_path / "delta.sqlite"
    info = build_delta(prev, curr, delta, from_build_id="b0", to_build_id="b1")
    # job 2 changed + job 4 new = 2 upserts; job 3 gone = 1 delete (job 1 identical -> skipped)
    assert info["upserts"] == 2
    assert info["deletes"] == 1
    assert info["from_build_id"] == "b0" and info["to_build_id"] == "b1"


def test_apply_delta_makes_prev_equivalent_to_curr(tmp_path):
    prev = tmp_path / "prev.sqlite"
    curr = tmp_path / "curr.sqlite"
    build_index(
        [
            _job("1", "Stripe", "Backend Engineer"),
            _job("2", "Ramp", "Frontend Engineer"),
            _job("3", "OpenAI", "ML Engineer"),
        ],
        prev,
        build_id="b0",
    )
    build_index(
        [
            _job("1", "Stripe", "Backend Engineer"),
            _job("2", "Ramp", "Senior Frontend Engineer"),  # changed
            _job("4", "NewCo", "Founding Engineer"),  # new
        ],
        curr,
        build_id="b1",
    )
    delta = tmp_path / "delta.sqlite"
    build_delta(prev, curr, delta, from_build_id="b0", to_build_id="b1")

    # apply the delta onto a COPY of prev -> must equal curr exactly
    applied = tmp_path / "applied.sqlite"
    applied.write_bytes(prev.read_bytes())
    apply_delta(applied, delta)

    aj, ac, abid = _snapshot(applied)
    cj, cc, cbid = _snapshot(curr)
    assert aj == cj  # same ids, same content_hash, same titles
    assert ac == cc  # company open_roles re-aggregated identically (OpenAI gone, NewCo added)
    assert abid == cbid == "b1"  # build_id advanced to the delta target

    # FTS still queryable + integrity intact on the applied index
    con = connect(applied, read_only=True)
    try:
        hit = con.execute(
            "SELECT j.title FROM jobs j JOIN jobs_fts f ON j.rowid=f.rowid "
            "WHERE jobs_fts MATCH 'founding'"
        ).fetchone()
        assert hit and hit[0] == "Founding Engineer"  # new row is searchable
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_apply_delta_rejects_wrong_base_build(tmp_path):
    # A delta is from_build_id -> to_build_id; applying to a base at a DIFFERENT build must refuse.
    prev = tmp_path / "prev.sqlite"
    other = tmp_path / "other.sqlite"
    curr = tmp_path / "curr.sqlite"
    build_index([_job("1", "Stripe", "Eng")], prev, build_id="b0")
    build_index([_job("1", "Stripe", "Eng"), _job("2", "Ramp", "Eng")], curr, build_id="b1")
    build_index([_job("9", "Zzz", "Eng")], other, build_id="bX")  # unrelated base
    delta = tmp_path / "delta.sqlite"
    build_delta(prev, curr, delta, from_build_id="b0", to_build_id="b1")

    import pytest

    with pytest.raises(Exception):  # noqa: B017 - any refusal is acceptable; base build mismatch
        apply_delta(other, delta)


def test_sequential_deltas_chain_to_fresh_build(tmp_path):
    # v2.2 foundation: a user several builds behind can apply consecutive deltas in sequence and
    # reach the latest index. apply_delta's from_build_id guard enforces correct ordering, so
    # chaining is just repeated application. Verify the chained result == a fresh build of the latest.
    b0 = [
        _job("1", "Stripe", "Backend Engineer"),
        _job("2", "Ramp", "Frontend Engineer"),
        _job("3", "OpenAI", "ML Engineer"),
    ]
    b1 = [
        _job("1", "Stripe", "Backend Engineer"),
        _job("2", "Ramp", "Staff Frontend Engineer"),  # changed
        _job("4", "Cursor", "Founding Engineer"),  # added (OpenAI dropped)
    ]
    b2 = [
        _job("1", "Stripe", "Senior Backend Engineer"),  # changed
        _job("4", "Cursor", "Founding Engineer"),
        _job("5", "Anthropic", "Research Engineer"),  # added (Ramp dropped)
    ]
    p0, p1, p2 = (tmp_path / f"b{i}.sqlite" for i in range(3))
    build_index(b0, p0, build_id="b0")
    build_index(b1, p1, build_id="b1")
    build_index(b2, p2, build_id="b2")
    d01, d12 = tmp_path / "d01.sqlite", tmp_path / "d12.sqlite"
    build_delta(p0, p1, d01, from_build_id="b0", to_build_id="b1")
    build_delta(p1, p2, d12, from_build_id="b1", to_build_id="b2")

    # user at b0 applies the chain d01 -> d12
    applied = tmp_path / "applied.sqlite"
    applied.write_bytes(p0.read_bytes())
    apply_delta(applied, d01)
    apply_delta(applied, d12)

    aj, ac, abid = _snapshot(applied)
    cj, cc, cbid = _snapshot(p2)
    assert aj == cj  # ids + content_hash + titles identical to a fresh b2 build
    assert ac == cc  # companies re-aggregated identically
    assert abid == cbid == "b2"


def test_delta_chain_rejects_out_of_order_application(tmp_path):
    # Applying a delta whose from_build_id != base build_id must refuse (keeps a chain consistent).
    import pytest

    b0 = [_job("1", "Stripe", "Eng")]
    b1 = [_job("1", "Stripe", "Eng"), _job("2", "Ramp", "Eng")]
    b2 = [_job("1", "Stripe", "Eng"), _job("2", "Ramp", "Eng"), _job("3", "X", "Eng")]
    p0, p1, p2 = (tmp_path / f"b{i}.sqlite" for i in range(3))
    build_index(b0, p0, build_id="b0")
    build_index(b1, p1, build_id="b1")
    build_index(b2, p2, build_id="b2")
    d12 = tmp_path / "d12.sqlite"
    build_delta(p1, p2, d12, from_build_id="b1", to_build_id="b2")
    # base is still at b0 -> applying d12 (needs b1) must refuse, not silently corrupt
    applied = tmp_path / "applied.sqlite"
    applied.write_bytes(p0.read_bytes())
    with pytest.raises(Exception):  # noqa: B017
        apply_delta(applied, d12)
