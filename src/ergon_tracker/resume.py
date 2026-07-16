"""Résumé convenience: read a résumé from a file and rank open roles by semantic fit.

The ranking core (``rank_by_resume``) is shared by the MCP ``match_resume`` tool and the CLI
``match-resume`` command so both rank a résumé identically. ``read_resume_file`` is the extra the
CLI/SDK need to accept a file path instead of pasted text (plain-text directly; PDF via optional
``pypdf``). Everything is served from the prebuilt index — zero ATS calls.
"""

from __future__ import annotations

from pathlib import Path

from .models import JobPosting, SearchQuery

__all__ = ["read_resume_file", "rank_by_resume"]


def read_resume_file(path: str | Path) -> str:
    """Read résumé text from a file.

    Plain-text résumés (``.txt``/``.md``/``.rst``/no suffix) are read directly. ``.pdf`` needs the
    optional ``pypdf`` package and raises a clear, actionable error if it is absent. Returns the
    extracted text (stripped). Raises ``FileNotFoundError`` if the path is not a file.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"résumé file not found: {p}")
    if p.suffix.lower() == ".pdf":
        return _read_pdf(p)
    return p.read_text(encoding="utf-8", errors="replace").strip()


def _read_pdf(p: Path) -> str:
    """Extract text from a PDF via ``pypdf`` (optional dep). Clear error when it is not installed."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pypdf is not a hard dependency — guide the user
        raise ImportError(
            "reading a PDF résumé needs the optional 'pypdf' package "
            "(pip install pypdf) — or convert the résumé to .txt/.md first"
        ) from exc
    reader = PdfReader(str(p))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def rank_by_resume(
    resume: str, query: SearchQuery, limit: int
) -> tuple[list[JobPosting] | None, str]:
    """Rank index candidates by fit to ``resume``; return ``(ranked_jobs, ranked_by_label)``.

    ``ranked_jobs`` is ``None`` when the prebuilt index is unavailable, ``[]`` when the filters
    matched nothing. A wide, filtered candidate pool is retrieved from the index, then the WHOLE pool
    is reranked by the full résumé — semantically (cosine) when the ``semantic`` extra is present,
    else lexically. Each returned job's ``.score`` is set in place. Callers issuing this must set
    ``query.limit`` to the user's desired result count (used to size the retrieval pool).
    """
    from .index.router import try_index

    # Wide, filtered candidate pool from the index; then rerank the WHOLE pool by the full résumé.
    pool = try_index(query.model_copy(update={"limit": max(limit * 8, 120)}))
    if pool is None:
        return None, ""
    if not pool:
        return [], "semantic_fit"

    ranked_by = "semantic_fit"
    try:
        from .semantic import get_semantic_reranker

        scores = get_semantic_reranker().rerank(resume, pool)
        for j, s in zip(pool, scores, strict=True):
            j.score = round(float(s), 4)
    except Exception:  # noqa: BLE001 - semantic extra absent / model error -> lexical fallback
        from .ranking import rank

        rank(pool, query.keywords or resume, reranker=None)  # sets lexical job.score in place

        ranked_by = "lexical (install the 'semantic' extra for embedding fit)"

    ranked = sorted(pool, key=lambda j: j.score if j.score is not None else 0.0, reverse=True)[
        :limit
    ]
    return ranked, ranked_by
