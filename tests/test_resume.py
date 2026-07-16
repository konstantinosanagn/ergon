"""Tests for the résumé-file convenience: read_resume_file + the shared rank_by_resume core."""

from __future__ import annotations

import pytest

from ergon_tracker.models import JobPosting, Location, RemoteType, SearchQuery
from ergon_tracker.resume import rank_by_resume, read_resume_file


def _job(title):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=title,
        company="Co",
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
    )


class _FakeReranker:
    def __init__(self, fit):
        self.fit = fit

    def rerank(self, query, jobs):
        return [self.fit.get(j.title, 0.0) for j in jobs]


def test_read_resume_file_reads_plaintext(tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text("Senior backend engineer, Python, Kubernetes.\n", encoding="utf-8")
    assert "backend engineer" in read_resume_file(p).lower()


def test_read_resume_file_reads_markdown(tmp_path):
    p = tmp_path / "cv.md"
    p.write_text("# Jane Doe\n\n- Built ML pipelines in PyTorch\n", encoding="utf-8")
    assert "pytorch" in read_resume_file(str(p)).lower()


def test_read_resume_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_resume_file(tmp_path / "nope.txt")


def test_read_resume_file_pdf_without_pypdf_is_actionable(tmp_path, monkeypatch):
    # Simulate pypdf being absent: the error must name the fix, not leak an ImportError deep in a lib.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "cv.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ImportError, match="pypdf"):
        read_resume_file(p)


def test_rank_by_resume_ranks_semantically(monkeypatch):
    pool = [_job("Backend Engineer"), _job("ML Engineer"), _job("Sales Rep")]
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: list(pool))
    monkeypatch.setattr(
        "ergon_tracker.semantic.get_semantic_reranker",
        lambda *a, **k: _FakeReranker(
            {"ML Engineer": 0.91, "Backend Engineer": 0.5, "Sales Rep": 0.1}
        ),
    )
    ranked, ranked_by = rank_by_resume("I build ML pipelines", SearchQuery(limit=2), 2)
    assert ranked_by == "semantic_fit"
    assert [j.title for j in ranked] == ["ML Engineer", "Backend Engineer"]


def test_rank_by_resume_index_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: None)
    ranked, _ = rank_by_resume("x", SearchQuery(limit=5), 5)
    assert ranked is None


def test_rank_by_resume_empty_pool_returns_empty(monkeypatch):
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: [])
    ranked, _ = rank_by_resume("x", SearchQuery(limit=5), 5)
    assert ranked == []
