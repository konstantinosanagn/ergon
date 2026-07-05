import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_index import (  # noqa: E402
    _crawl_network,
    _fold_network_into_fresh,
    _interleave_by_ats,
    publish_artifacts,
)

from ergon_tracker.index.build import build_index  # noqa: E402
from ergon_tracker.models import JobPosting  # noqa: E402


def test_interleave_by_ats_balances_clustered_registry():
    from collections import Counter

    # Insertion order clusters one backend (1000 workable, then 50 gh, 50 lever) — the exact shape
    # that caused the 429 storm. Interleaving must spread them so an early window isn't all workable.
    items = [(f"wk{i}", {"ats": "workable", "token": f"wk{i}"}) for i in range(1000)]
    items += [(f"gh{i}", {"ats": "greenhouse", "token": f"gh{i}"}) for i in range(50)]
    items += [(f"lv{i}", {"ats": "lever", "token": f"lv{i}"}) for i in range(50)]

    out = _interleave_by_ats(items)
    assert len(out) == len(items)
    assert {k for k, _ in out} == {k for k, _ in items}  # permutation: nothing lost/duplicated

    # workable is ~91% of the registry; EVERY 100-wide window (head, middle, AND tail) must stay
    # near that share — never a 100%-one-backend burst, at any offset. (A round-robin interleave
    # passes the head but fails the tail; stratified placement passes everywhere.)
    for start in (0, 500, 900, 1000):  # 1000 = the tail, where naive interleave clustered
        w = Counter(e["ats"] for _, e in out[start : start + 100])
        assert w["workable"] <= 96, f"window@{start} workable-dominated: {dict(w)}"
        # minority backends keep appearing throughout, not just at the front
        assert (w["greenhouse"] + w["lever"]) >= 1, f"window@{start} has no minority ATS: {dict(w)}"


def test_interleave_single_ats_is_identity_set():
    items = [(f"a{i}", {"ats": "greenhouse", "token": f"a{i}"}) for i in range(5)]
    out = _interleave_by_ats(items)
    assert [k for k, _ in out] == [k for k, _ in items]


def test_crawl_network_disabled_returns_empty():
    import anyio

    assert anyio.run(_crawl_network, 0) == []


def test_crawl_network_folds_in_workable_network_jobs():
    import anyio
    import httpx
    import respx

    page = {
        "jobs": [
            {
                "id": "n1",
                "title": "Platform Engineer",
                "company": {"title": "NetCo", "website": "https://netco.com"},
                "workplace": "remote",
                "employmentType": "Full-time",
                "location": {"city": "Lisbon", "subregion": None, "countryName": "Portugal"},
                "created": "2026-06-19T00:00:00.000Z",
                "url": "https://jobs.workable.com/view/n1/x",
                "description": "<p>Build.</p>",
            }
        ],
        "nextPageToken": None,
    }
    with respx.mock:
        respx.get("https://jobs.workable.com/api/v1/jobs").mock(
            return_value=httpx.Response(200, json=page)
        )
        jobs = anyio.run(_crawl_network, 1)
    assert len(jobs) == 1
    assert jobs[0].source == "workable_network"
    assert jobs[0].company == "NetCo"
    assert jobs[0].title == "Platform Engineer"


def test_fold_network_into_fresh_appends_and_returns_keys(tmp_path):
    import anyio
    import httpx
    import respx

    from ergon_tracker.dedup import normalize_company
    from ergon_tracker.index.db import connect, fresh_db

    fresh = tmp_path / "fresh.sqlite"
    fresh_db(fresh)  # create the index schema the incremental crawl streams into

    page = {
        "jobs": [
            {
                "id": "fn1",
                "title": "SRE",
                "company": {"title": "FoldCo", "website": "https://foldco.com"},
                "workplace": "remote",
                "employmentType": "Full-time",
                "location": {"city": "Austin", "subregion": None, "countryName": "United States"},
                "created": "2026-06-19T00:00:00.000Z",
                "url": "https://jobs.workable.com/view/fn1/x",
                "description": "<p>Run things.</p>",
            }
        ],
        "nextPageToken": None,
    }
    with respx.mock:
        respx.get("https://jobs.workable.com/api/v1/jobs").mock(
            return_value=httpx.Response(200, json=page)
        )
        keys = anyio.run(_fold_network_into_fresh, fresh, 1, "build-test")

    assert keys == {normalize_company("FoldCo")}
    con = connect(fresh)
    try:
        n = con.execute("SELECT COUNT(*) FROM jobs WHERE source='workable_network'").fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_fold_network_into_fresh_noop_when_disabled(tmp_path):
    import anyio

    from ergon_tracker.index.db import fresh_db

    fresh = tmp_path / "fresh.sqlite"
    fresh_db(fresh)
    assert anyio.run(_fold_network_into_fresh, fresh, 0, "build-test") == set()


def test_publish_writes_gz_and_manifest(tmp_path):
    src = tmp_path / "i.sqlite"
    build_index(
        [JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Eng")],
        src,
        build_id="b1",
    )
    out = tmp_path / "dist"
    publish_artifacts(src, out, build_id="b1")
    man = json.loads((out / "manifest.json").read_text())
    from ergon_tracker.index.db import SCHEMA_VERSION

    assert man["build_id"] == "b1" and man["schema_version"] == SCHEMA_VERSION
    raw = gzip.decompress((out / "index.sqlite.gz").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == man["sha256"]


def test_append_history_accumulates(tmp_path):
    from build_index import append_history

    h = tmp_path / "runs" / "history.jsonl"
    append_history(h, {"build_id": "b1", "total_jobs": 10})
    append_history(h, {"build_id": "b2", "total_jobs": 12})
    import json

    rows = [json.loads(line) for line in h.read_text().splitlines()]
    assert [r["build_id"] for r in rows] == ["b1", "b2"]


def test_build_and_publish_shards_gzips(tmp_path):
    from build_index import build_and_publish_shards

    from ergon_tracker.models import JobPosting

    jobs = [
        JobPosting.create(
            source="greenhouse",
            source_job_id=str(i),
            company=f"Co{i}",
            title="Engineer",
            sector=("Fintech" if i % 2 else None),
        )
        for i in range(4)
    ]
    n = build_and_publish_shards(jobs, tmp_path, build_id="b1")
    assert n == 2  # fintech + unknown
    assert (tmp_path / "shards.json").exists()
    gzs = {f.name for f in tmp_path.glob("shard-*.sqlite.gz")}
    assert gzs == {"shard-fintech.sqlite.gz", "shard-unknown.sqlite.gz"}


def test_new_boards_selects_unseen_and_caps():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "_bi", pathlib.Path(__file__).parent.parent / "scripts" / "build_index.py"
    )
    bi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bi)
    from ergon_tracker.index.scheduler import BoardState

    items = [
        ("a", {"ats": "greenhouse", "token": "acme"}),
        ("b", {"ats": "lever", "token": "beta"}),
        ("c", {"ats": "ashby", "token": "gamma"}),
        ("d", {"ats": None, "token": None}),  # not crawlable -> ignored
    ]
    # 'a' already has state -> only b,c are new
    seen = {BoardState(provider="greenhouse", token="acme").key: object()}
    new = bi._new_boards(items, seen)
    tokens = {e["token"] for _, e in new}
    assert tokens == {"beta", "gamma"}
    # cap is respected
    assert len(bi._new_boards(items, {}, cap=1)) == 1


def test_deltas_window_accumulates_contiguous_chain_across_builds(tmp_path):
    import importlib.util
    import json
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "_bi2", pathlib.Path(__file__).parent.parent / "scripts" / "build_index.py"
    )
    bi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bi)
    from ergon_tracker.index.build import build_index
    from ergon_tracker.models import JobPosting

    def _job(sid, title):
        return JobPosting.create(source="greenhouse", source_job_id=sid, company="Co", title=title)

    out = tmp_path / "dist"
    out.mkdir()
    b1, b2, b3 = (tmp_path / f"b{i}.sqlite" for i in (1, 2, 3))
    build_index([_job("1", "Backend Engineer")], b1, build_id="build-1")
    build_index(
        [_job("1", "Backend Engineer"), _job("2", "Frontend Engineer")], b2, build_id="build-2"
    )
    build_index(
        [_job("1", "Backend Engineer"), _job("2", "Frontend Engineer"), _job("3", "ML Engineer")],
        b3,
        build_id="build-3",
    )

    bi.build_and_publish_delta(b1, b2, out, build_id="build-2")
    bi.build_and_publish_delta(b2, b3, out, build_id="build-3")

    window = json.loads((out / "deltas.json").read_text())["deltas"]
    # two entries forming a contiguous build-1 -> build-2 -> build-3 chain
    assert [(d["from_build_id"], d["to_build_id"]) for d in window] == [
        ("build-1", "build-2"),
        ("build-2", "build-3"),
    ]
    # each entry's per-build chain file was actually written
    for d in window:
        assert (out / d["file"]).exists()
    # the generic 1-behind delta points at the latest step
    assert json.loads((out / "manifest-delta.json").read_text())["to_build_id"] == "build-3"
