"""Layer-2 semantic search wiring (no model download — uses a fake reranker)."""

from __future__ import annotations

import importlib.util

import pytest

from ergon_tracker.models import JobPosting, Location, SearchQuery
from ergon_tracker.ranking import rank

_HAS_FASTEMBED = importlib.util.find_spec("fastembed") is not None


def _job(title: str, *, description: str | None = None) -> JobPosting:
    return JobPosting.create(
        source="greenhouse",
        source_job_id=title,
        company="Acme",
        title=title,
        description_text=description,
        locations=[Location(raw="Remote")],
    )


def test_semantic_flag_skips_keyword_gate() -> None:
    # In lexical mode, a job with no token overlap is filtered out...
    job = _job("Machine Learning Specialist", description="We build models.")
    assert SearchQuery(keywords="ML engineer").matches(job) is False
    # ...but semantic mode keeps it (ranking decides relevance, not an exact-token gate).
    assert SearchQuery(keywords="ML engineer", semantic=True).matches(job) is True


def test_semantic_other_filters_still_apply() -> None:
    job = _job("ML Engineer")  # location Remote
    q = SearchQuery(keywords="anything", semantic=True, location="Berlin")
    assert q.matches(job) is False  # location filter still enforced in semantic mode


def test_per_call_reranker_reorders_results() -> None:
    # A fake reranker proves the seam: it scores by a keyword the lexical layer ignores.
    class FakeSemantic:
        def rerank(self, query, jobs):
            return [1.0 if "designer" in j.title.lower() else 0.0 for j in jobs]

    eng = _job("Software Engineer")
    des = _job("Product Designer")
    ranked = rank([eng, des], "creative role", reranker=FakeSemantic())
    assert ranked[0].title == "Product Designer"  # reranker won, not lexical


def test_get_semantic_reranker_importable() -> None:
    from ergon_tracker.semantic import DEFAULT_MODEL, get_semantic_reranker

    r = get_semantic_reranker()
    assert r.model_name == DEFAULT_MODEL
    # Memoized: same instance back.
    assert get_semantic_reranker() is r


@pytest.mark.skipif(_HAS_FASTEMBED, reason="extra installed; error path only fires without it")
def test_helpful_error_without_extra() -> None:
    from ergon_tracker.semantic import get_semantic_reranker

    with pytest.raises(ImportError, match="ergon-tracker\\[semantic\\]"):
        get_semantic_reranker().rerank("data", [_job("Data Engineer")])
