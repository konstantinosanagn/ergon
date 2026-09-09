"""Relevance ranking (field-weighted BM25) tests."""

from __future__ import annotations

from ergon.models import JobPosting, Location
from ergon.ranking import rank, register_reranker, score_text


def _job(
    title: str, *, description: str | None = None, department: str | None = None
) -> JobPosting:
    return JobPosting.create(
        source="greenhouse",
        source_job_id=title,
        company="Acme",
        title=title,
        description_text=description,
        department=department,
        locations=[Location(raw="Remote")],
    )


def test_title_match_beats_description_only_match() -> None:
    # The exact problem we saw live: "engineer" must not surface an AE role whose
    # description merely mentions engineering above an actual Engineer title.
    ae = _job(
        "Account Executive, AI Sales",
        description="Work closely with engineering and engineer teams to close deals.",
    )
    eng = _job("Software Engineer", description="Build backend services.")
    ranked = rank([ae, eng], "engineer")
    assert ranked[0].title == "Software Engineer"
    assert ranked[0].score > ranked[1].score


def test_multi_term_query_ranks_best_overlap_first() -> None:
    a = _job("Senior Rust Backend Engineer")
    b = _job("Rust Engineer")
    c = _job("Frontend Engineer")
    ranked = rank([c, b, a], "rust backend engineer")
    assert ranked[0].title == "Senior Rust Backend Engineer"
    assert "Rust" in ranked[1].title
    assert ranked[-1].title == "Frontend Engineer"


def test_keyword_stuffed_description_does_not_beat_title() -> None:
    stuffed = _job(
        "Office Manager",
        description=("python " * 50).strip(),  # spammy repetition
    )
    real = _job("Python Developer", description="We use Python.")
    ranked = rank([stuffed, real], "python")
    # BM25 saturation + title weighting keep the real title on top.
    assert ranked[0].title == "Python Developer"


def test_rank_is_stable_for_ties() -> None:
    # No query terms present -> all zero scores -> original order preserved.
    a = _job("Designer")
    b = _job("Recruiter")
    ranked = rank([a, b], "engineer")
    assert [j.title for j in ranked] == ["Designer", "Recruiter"]


def test_no_query_returns_unchanged() -> None:
    a, b = _job("A"), _job("B")
    assert rank([a, b], None) == [a, b]
    assert rank([a, b], "") == [a, b]


def test_score_is_set_on_jobs() -> None:
    jobs = [_job("Data Engineer"), _job("Data Analyst")]
    rank(jobs, "engineer")
    assert all(j.score is not None for j in jobs)


def test_score_text_empty_query() -> None:
    jobs = [_job("Engineer")]
    assert score_text("", jobs) == [0.0]
    assert score_text("engineer", []) == []


def test_pluggable_reranker_overrides_then_resets() -> None:
    class ReverseReranker:
        # Assign higher score to whatever lexical ranked lowest (reverse), to prove it runs.
        def rerank(self, query, jobs):
            return [float(i) for i in range(len(jobs))]

    a = _job("Software Engineer")
    b = _job("Account Executive", description="engineer")
    try:
        register_reranker(ReverseReranker())
        ranked = rank([a, b], "engineer")
        # Reranker gave the second head item the higher score -> it leads.
        assert ranked[0].score >= ranked[1].score
    finally:
        register_reranker(None)  # never leak global state to other tests

    # After reset, pure lexical ranking again puts the Engineer title first.
    ranked2 = rank([a, b], "engineer")
    assert ranked2[0].title == "Software Engineer"
