"""Parity check: verify the LIVE filter path (``SearchQuery.matches()``, applied to a
``JobPosting`` fetched straight from an ATS) and the prebuilt-index SQL path
(``ergon_tracker.index.query.search_rows`` / its ``_where`` clause builder, run against the
sqlite snapshot) reach the SAME accept/reject decision for a given ``(query, job)`` pair -- i.e. a
user gets the same jobs whether they search live or search the index.

Method: rather than reimplementing ``_where()``'s SQL semantics in Python (which would just test
one hand-written mirror against another and could silently drift from the real clause builder),
this module runs the REAL SQL path: the REAL ``to_row()`` mapping
(``ergon_tracker.index.mapping.to_row`` -- the exact function ``build_index`` uses) turns ``job``
into a single index row in a throwaway in-memory sqlite table (plus its ``jobs_fts`` index, built
the same way ``build_index`` builds it), and the REAL ``search_rows()`` runs against it unchanged.
No fixture files, no ``build_index()`` call, no network -- a scratch one-row "index" is
microseconds to build and tear down.

Two divergences between the two paths are BY DESIGN, not bugs, and are excluded from
``agree()``'s structured-filter comparison (reported separately instead):

* ``keywords`` -- ``matches()`` does a plain "every token is a substring of
  title+department+company+description_text" check; the index runs keywords through FTS5 MATCH
  (``_match_expr``: AND for 1-2 tokens, phrase-OR-NEAR for 3-4, +any-token OR for 5+ -- see
  ``index/query.py``'s module docstring). These are different retrieval semantics on purpose
  (precision-tuned ranking vs an exact gate), not a parity bug. ``divergence_rate_keywords()``
  measures how often that difference actually flips the accept/reject outcome on a sample. One
  structural source of divergence worth knowing: the FTS content only covers a ``snippet`` (the
  first 300 chars of ``description_text``, see ``mapping._SNIPPET``), so a keyword appearing only
  later in a long description can match client-side (full text) but miss server-side (truncated
  snippet) -- this alone will show up as unavoidable non-zero divergence on real long-JD corpora.
* ``max_last_seen_age_days`` -- SQL-only. It keys on the index's own ``last_seen`` column (the
  last build that re-confirmed the posting present on its board), a concept a live-fetched
  ``JobPosting`` has no field for at all. ``matches()`` has no corresponding check and silently
  accepts regardless of this filter. ``flag_sql_only_filters()`` reports whenever a query sets it
  -- such a query's result can never be verified against the live path with this harness (or any
  harness: the field simply doesn't exist off the index).
"""

from __future__ import annotations

import sqlite3

from ergon_tracker.index.mapping import to_row
from ergon_tracker.index.query import search_rows
from ergon_tracker.models import JobPosting, SearchQuery

__all__ = [
    "SQL_ONLY_FIELDS",
    "check_row",
    "sql_accepts",
    "agree",
    "flag_sql_only_filters",
    "divergence_rate_keywords",
]

# SearchQuery fields the index SQL (`_where`) implements that `matches()` has NO client-side
# equivalent for -- see module docstring. `matches()` silently ignores them.
SQL_ONLY_FIELDS: tuple[str, ...] = ("max_last_seen_age_days",)

# fts5 external-content definition, copied verbatim from index/schema.sql so the scratch table
# indexes the same four columns (title, company, department, snippet) the same way the real
# index does. Kept here (not re-parsed from schema.sql) so this module has no filesystem
# dependency at import time.
_FTS_DDL = (
    "CREATE VIRTUAL TABLE jobs_fts USING fts5(title, company, department, snippet, "
    "content='jobs', content_rowid='rowid', "
    'tokenize="porter unicode61 remove_diacritics 2")'
)


def _scratch_index(job: JobPosting) -> sqlite3.Connection:
    """A one-row, in-memory sqlite 'index' for ``job``: the REAL ``to_row()`` mapping inserted
    into a ``jobs`` table, plus a real ``jobs_fts`` index built the same way ``build_index()``
    builds it (``INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')``). Callers run the REAL
    ``search_rows()`` against the returned connection -- this is the genuine index SQL path, just
    scoped to a single row so no on-disk index or migration is needed.
    """
    row = to_row(job, build_id="parity")
    con = sqlite3.connect(":memory:")
    cols = ", ".join(f'"{c}"' for c in row)
    con.execute(f"CREATE TABLE jobs ({cols})")
    con.execute(
        f"INSERT INTO jobs ({cols}) VALUES ({', '.join('?' for _ in row)})",
        list(row.values()),
    )
    con.execute(_FTS_DDL)
    con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
    return con


def check_row(query: SearchQuery, job: JobPosting) -> bool:
    """Whether the LIVE/client-side path (``SearchQuery.matches()``) accepts ``job`` for
    ``query``."""
    return query.matches(job)


def sql_accepts(query: SearchQuery, job: JobPosting) -> bool:
    """Whether the INDEX SQL path accepts ``job`` for ``query``: builds a throwaway one-row
    scratch index for ``job`` (see ``_scratch_index``) and runs the REAL ``search_rows()`` against
    it, unchanged."""
    con = _scratch_index(job)
    try:
        return len(search_rows(con, query)) > 0
    finally:
        con.close()


def agree(query: SearchQuery, job: JobPosting) -> bool:
    """Whether the client (``check_row``) and index-SQL (``sql_accepts``) paths reach the SAME
    accept/reject decision on ``job`` for ``query``'s STRUCTURED filters (level, country, city,
    remote, sector, employment_type, salary, years, degree, sponsorship, visa, recency).

    ``keywords`` is stripped from ``query`` on both sides before comparing -- it diverges by
    design (see module docstring) and is scored separately by ``divergence_rate_keywords``, not
    folded into this pass/fail. ``max_last_seen_age_days`` is NOT stripped (it's a real filter the
    SQL side still applies); callers should pair this with ``flag_sql_only_filters`` when that
    field is set, since a mismatch there reflects a structural gap, not a bug in either path.
    """
    q = query.model_copy(update={"keywords": None})
    return check_row(q, job) == sql_accepts(q, job)


def flag_sql_only_filters(query: SearchQuery) -> list[str]:
    """Which SQL-only filters ``query`` sets (currently just ``max_last_seen_age_days``) that
    ``matches()`` has no client-side equivalent for -- see module docstring. These can never be
    verified against the live path and should be called out, not silently trusted."""
    return [f for f in SQL_ONLY_FIELDS if getattr(query, f) is not None]


def divergence_rate_keywords(pairs: list[tuple[SearchQuery, JobPosting]]) -> float:
    """Fraction of ``(query, job)`` pairs -- restricted to those where ``query.keywords`` is set
    -- where the client substring gate and the index FTS5 MATCH disagree on accept/reject. This is
    the BY-DESIGN divergence documented in the module docstring, measured rather than asserted:
    ``check_row``/``sql_accepts`` are run WITHOUT stripping keywords, unlike ``agree()``.

    Returns ``0.0`` (not undefined) when no pair in ``pairs`` sets keywords -- there's nothing to
    diverge on.
    """
    kw_pairs = [(q, j) for q, j in pairs if q.keywords]
    if not kw_pairs:
        return 0.0
    disagreements = sum(1 for q, j in kw_pairs if check_row(q, j) != sql_accepts(q, j))
    return disagreements / len(kw_pairs)
