import ergon_tracker.index.router as router
from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.models import JobLevel, JobPosting, SearchQuery


def test_router_uses_index_for_broad_query(tmp_path, monkeypatch):
    p = tmp_path / "i.sqlite"
    build_index(
        [
            JobPosting.create(
                source="greenhouse",
                source_job_id="1",
                company="Co",
                title="Senior Backend Engineer",
                level=JobLevel.SENIOR,
            )
        ],
        p,
        build_id="b1",
    )
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)  # no shards -> single-file path
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))
    out = router.try_index(SearchQuery(keywords="backend", limit=5))
    assert out is not None and out[0].title == "Senior Backend Engineer"


def test_router_returns_none_for_targeted_query():
    assert router.try_index(SearchQuery(keywords="x", companies=["stripe.com"])) is None


def test_router_returns_none_when_index_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(
        router, "_load_backend", lambda: SqliteIndexBackend(tmp_path / "none.sqlite")
    )
    assert router.try_index(SearchQuery(keywords="x")) is None


def test_router_prefers_sharded_over_single_file(tmp_path, monkeypatch):
    from ergon_tracker.index.backend import ShardedIndexBackend
    from ergon_tracker.index.build import build_sharded_index

    build_sharded_index(
        [
            JobPosting.create(
                source="greenhouse",
                source_job_id="1",
                company="Stripe",
                title="Backend Engineer",
                sector="Fintech",
            )
        ],
        tmp_path,
        build_id="b1",
    )
    monkeypatch.setattr(router, "_load_sharded", lambda q: ShardedIndexBackend(tmp_path))
    # single-file should NOT be consulted when shards serve the query
    monkeypatch.setattr(
        router, "_load_backend", lambda: (_ for _ in ()).throw(AssertionError("used single-file"))
    )
    out = router.try_index(SearchQuery(keywords="backend", sector="Fintech", limit=5))
    assert out is not None and out[0].company == "Stripe"


def test_router_skips_shards_for_broad_query(tmp_path, monkeypatch):
    # A no-sector (broad) query must NOT touch the sharded path: pulling all shards is slower
    # than the single-file index's one download + global FTS rank. Sharding only helps sectors.
    p = tmp_path / "i.sqlite"
    build_index(
        [
            JobPosting.create(
                source="greenhouse",
                source_job_id="1",
                company="Co",
                title="Senior Backend Engineer",
                level=JobLevel.SENIOR,
            )
        ],
        p,
        build_id="b1",
    )
    monkeypatch.setattr(
        router,
        "_load_sharded",
        lambda q: (_ for _ in ()).throw(AssertionError("shards consulted for broad query")),
    )
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))
    out = router.try_index(SearchQuery(keywords="backend", limit=5))  # no sector
    assert out is not None and out[0].title == "Senior Backend Engineer"


def test_router_prefers_slim_for_broad_filter_query(tmp_path, monkeypatch):
    # A broad structured-filter query (no keywords/years/semantic) must use the slim tier and
    # NOT download the full single-file index.
    p = tmp_path / "slim.sqlite"
    build_index(
        [
            JobPosting.create(
                source="greenhouse",
                source_job_id="1",
                company="Co",
                title="Senior Backend Engineer",
                level=JobLevel.SENIOR,
            )
        ],
        p,
        build_id="b1",
    )
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(router, "_load_slim", lambda: SqliteIndexBackend(p))
    monkeypatch.setattr(
        router, "_load_backend", lambda: (_ for _ in ()).throw(AssertionError("used full index"))
    )
    out = router.try_index(SearchQuery(level=JobLevel.SENIOR, limit=5))  # no keywords
    assert out is not None and out[0].title == "Senior Backend Engineer"


def test_router_skips_slim_for_keyword_query(tmp_path, monkeypatch):
    # A keyword query may match in the description (nulled in slim) -> must use the full index.
    p = tmp_path / "full.sqlite"
    build_index(
        [JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Engineer")],
        p,
        build_id="b1",
    )
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(
        router, "_load_slim", lambda: (_ for _ in ()).throw(AssertionError("slim used for keyword"))
    )
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))
    out = router.try_index(SearchQuery(keywords="engineer", limit=5))
    assert out is not None


def test_router_serves_keyword_from_slim_when_opted_in(tmp_path, monkeypatch):
    # ERGON_INDEX=slim opts broad KEYWORD queries into the compact tier (title+company FTS) for a
    # ~3x smaller cold-start download; the full index must NOT be consulted.
    p = tmp_path / "slim.sqlite"
    build_index(
        [JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Engineer")],
        p,
        build_id="b1",
    )
    monkeypatch.setenv("ERGON_INDEX", "slim")
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(router, "_load_slim", lambda: SqliteIndexBackend(p))
    monkeypatch.setattr(
        router,
        "_load_backend",
        lambda: (_ for _ in ()).throw(AssertionError("full index used under ERGON_INDEX=slim")),
    )
    out = router.try_index(SearchQuery(keywords="engineer", limit=5))
    assert out is not None and out[0].title == "Engineer"


def test_slim_never_serves_year_or_semantic_even_when_opted_in(monkeypatch):
    # Year-filtered / semantic queries lack the data in slim (years nulled, no vectors): even under
    # ERGON_INDEX=slim they must fall through to the full index.
    from ergon_tracker.index.router import _slim_serves

    monkeypatch.setenv("ERGON_INDEX", "slim")
    assert _slim_serves(SearchQuery(keywords="eng", max_years=2)) is False
    assert _slim_serves(SearchQuery(keywords="eng", min_years=3)) is False
    assert _slim_serves(SearchQuery(keywords="eng", semantic=True)) is False
    # ...but a plain keyword query does opt in, and a broad filter query always does.
    assert _slim_serves(SearchQuery(keywords="eng")) is True
    assert _slim_serves(SearchQuery(level=JobLevel.SENIOR)) is True


def test_env_off_disables_index(monkeypatch):
    monkeypatch.setenv("ERGON_INDEX", "off")
    assert router.try_index(SearchQuery(keywords="x")) is None


def test_try_index_ranked_no_semantic_equals_try_index(tmp_path, monkeypatch):
    from ergon_tracker.index.backend import SqliteIndexBackend
    from ergon_tracker.index.build import build_index

    p = tmp_path / "i.sqlite"
    build_index(
        [
            JobPosting.create(
                source="greenhouse", source_job_id=str(i), company="Co", title=f"Engineer {i}"
            )
            for i in range(5)
        ],
        p,
        build_id="b1",
    )
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(router, "_load_slim", lambda: None)
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))
    q = SearchQuery(keywords="engineer", limit=10)
    assert {j.id for j in router.try_index_ranked(q)} == {j.id for j in router.try_index(q)}


def test_try_index_ranked_semantic_degrades_gracefully(tmp_path, monkeypatch):
    # semantic=True must still return index results even if the reranker is unavailable (the MCP +
    # engine both rely on this shared path; it must never crash a broad semantic query).
    from ergon_tracker.index.backend import SqliteIndexBackend
    from ergon_tracker.index.build import build_index

    p = tmp_path / "i.sqlite"
    build_index(
        [
            JobPosting.create(
                source="greenhouse", source_job_id=str(i), company="Co", title=f"ML Engineer {i}"
            )
            for i in range(5)
        ],
        p,
        build_id="b1",
    )
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(router, "_load_slim", lambda: None)
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))
    # force the reranker import to fail -> must fall back to lexical, not raise
    monkeypatch.setattr(
        "ergon_tracker.semantic.get_semantic_reranker",
        lambda: (_ for _ in ()).throw(RuntimeError("no fastembed")),
    )
    out = router.try_index_ranked(SearchQuery(keywords="ml", semantic=True, limit=10))
    assert out is not None and len(out) >= 1  # graceful lexical fallback
