"""Small, explicit retrieval judgments: no model download or external service required."""

from __future__ import annotations

import pytest

from ergon.index.backend import SqliteIndexBackend
from ergon.index.build import build_index
from ergon.index.db import connect
from ergon.index.query import whats_new_rows
from ergon.models import JobPosting, Location, SearchQuery


@pytest.fixture
def evaluated_index(tmp_path):
    titles = [
        ("cpp", "C++ Engineer", "Germany"),
        ("csharp", "C# Engineer", "Germany"),
        ("c", "C Engineer", "Germany"),
        ("ec", "E&C Manager", "Germany"),
        ("ml", "Machine Learning Engineer", "Germany"),
        ("abbr", "ML Engineer", "Canada"),
        ("nurse", "Clinical Nurse", "Germany"),
        ("unicode", "Инженер", "Germany"),
    ]
    jobs = [
        JobPosting.create(
            source="greenhouse",
            source_job_id=sid,
            company=f"Employer {i}",
            title=title,
            locations=[Location(raw=country, country=country)],
        )
        for i, (sid, title, country) in enumerate(titles)
    ]
    path = tmp_path / "eval.sqlite"
    build_index(jobs, path, build_id="evaluation")
    return SqliteIndexBackend(path)


@pytest.mark.parametrize(
    "query, expected",
    [
        (SearchQuery(keywords="C++", limit=1), {"cpp"}),
        (SearchQuery(keywords="C#", limit=1), {"csharp"}),
        (SearchQuery(keywords="C++ engineer"), {"cpp"}),
        (SearchQuery(keywords="C# engineer"), {"csharp"}),
        (SearchQuery(keywords="ML engineer", semantic=True), {"ml", "abbr"}),
        (SearchQuery(keywords="machine learning engineer", semantic=True), {"ml", "abbr"}),
        (SearchQuery(keywords="ML engineer", semantic=True, country="Germany"), {"ml"}),
        (SearchQuery(keywords="ML engineer", semantic=False), {"abbr"}),
        (SearchQuery(keywords="clinical nurse"), {"nurse"}),
        (SearchQuery(keywords="Инженер"), {"unicode"}),
        (SearchQuery(keywords="***"), set()),
        (SearchQuery(keywords='x" OR "nurse'), set()),
        (SearchQuery(keywords="ML infrastructure software engineer", semantic=True), set()),
    ],
)
def test_judged_retrieval(evaluated_index, query, expected):
    actual = {p.source_job_id for j in evaluated_index.search(query) for p in j.provenance}
    assert actual == expected


def test_change_feed_preserves_identifier(evaluated_index):
    con = connect(evaluated_index.path, read_only=True)
    try:
        rows = whats_new_rows(con, SearchQuery(keywords="C#", limit=1), "2000-01-01")
        assert [r["title"] for r in rows] == ["C# Engineer"]
    finally:
        con.close()


@pytest.mark.parametrize("incompatibility", ["model", "missing_model", "dimension"])
def test_incompatible_model_is_not_scored(tmp_path, monkeypatch, incompatibility):
    import sqlite3

    from tests.test_rich_index import FakeReranker, _job

    from ergon.index import router
    from ergon.index.rich import build_rich_tier

    jobs = [_job("a", "Python Engineer", "python"), _job("b", "Nurse", "nurse")]
    path = tmp_path / "vectors.sqlite"
    build_rich_tier(jobs, path, build_id="b", reranker=FakeReranker())
    other = FakeReranker()
    if incompatibility == "model":
        other.model_name = "another-384-dimensional-model"
    else:
        with sqlite3.connect(path) as con:
            if incompatibility == "missing_model":
                con.execute("DELETE FROM meta WHERE key='model'")
            else:
                con.execute("UPDATE meta SET value='128' WHERE key='dim'")
    monkeypatch.setattr(router, "_rich_path", lambda: path)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: other)
    assert router._vector_rerank(SearchQuery(keywords="python", semantic=True), jobs, 2) is None


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stored_model", ["fake-384", None])
def test_reconcile_rejects_unknown_or_different_model_before_mutation(
    tmp_path, streaming, stored_model
):
    import sqlite3

    from tests.test_rich_index import FakeReranker, _job

    from ergon.index.rich import (
        build_rich_tier,
        reconcile_rich_tier,
        reconcile_rich_tier_from_fresh,
        write_fresh_rich,
    )

    jobs = [_job("a", "Python Engineer", "python")]
    main = tmp_path / "main.sqlite"
    build_index(jobs, main, build_id="b1")
    path = tmp_path / "vectors.sqlite"
    build_rich_tier(jobs, path, build_id="b1", reranker=FakeReranker())
    with sqlite3.connect(path) as con:
        if stored_model is None:
            con.execute("DELETE FROM meta WHERE key='model'")
    before = path.read_bytes()
    other = FakeReranker()
    other.model_name = "another-384-dimensional-model"
    with pytest.raises(ValueError, match="Vector model mismatch"):
        if streaming:
            fresh = tmp_path / "fresh.sqlite"
            with sqlite3.connect(fresh) as con:
                write_fresh_rich(con, jobs)
            reconcile_rich_tier_from_fresh(path, main, fresh, build_id="b2", reranker=other)
        else:
            reconcile_rich_tier(path, main, jobs, build_id="b2", reranker=other)
    assert path.read_bytes() == before


def test_empty_vector_sidecar_acquires_dimension_when_populated(tmp_path):
    from tests.test_rich_index import FakeReranker, _job

    from ergon.index.rich import build_rich_tier, open_rich, reconcile_rich_tier, rich_meta

    jobs = [_job("a", "Python Engineer", "python")]
    main, vectors = tmp_path / "main.sqlite", tmp_path / "vectors.sqlite"
    build_index(jobs, main, build_id="b1")
    build_rich_tier([], vectors, build_id="empty", reranker=FakeReranker())
    reconcile_rich_tier(vectors, main, jobs, build_id="b1", reranker=FakeReranker())
    con = open_rich(vectors)
    try:
        assert rich_meta(con)["dim"] == "384"
        assert rich_meta(con)["model"] == "fake-384"
    finally:
        con.close()
