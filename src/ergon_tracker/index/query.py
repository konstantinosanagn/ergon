"""Translate a SearchQuery into SQL over the index, mirroring SearchQuery.matches() semantics."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..models import DEGREE_ORDER, SearchQuery

_TOKEN = re.compile(r"[a-z0-9]+")


def _match_expr(keywords: str) -> str:
    toks = _TOKEN.findall(keywords.lower())
    return " AND ".join(f'"{t}"' for t in toks)  # quoted = no FTS5 syntax injection


def _where(q: SearchQuery) -> tuple[list[str], list[Any]]:
    cl: list[str] = ["j.status = 'active'"]
    p: list[Any] = []
    if q.remote is True:
        # Mirror SearchQuery.matches(): remote/hybrid OR a "remote" signal in the location text.
        # Recovers postings tagged remote only in their location string (remote='unknown' but
        # location says "Remote") that an exact-column match would miss.
        cl.append("(j.remote IN ('remote','hybrid') OR LOWER(j.location) LIKE '%remote%')")
    if q.level is not None:
        if q.include_unknown_level:
            cl.append("(j.level = ? OR j.level = 'unknown')")
            p.append(q.level.value)
        else:
            cl.append("j.level = ?")
            p.append(q.level.value)
    if q.sector:
        if q.include_unknown_sector:
            cl.append("(LOWER(j.sector) LIKE ? OR j.sector IS NULL)")
            p.append(f"%{q.sector.lower()}%")
        else:
            cl.append("LOWER(j.sector) LIKE ?")
            p.append(f"%{q.sector.lower()}%")
    if q.country:
        # Alias-resolve the query (USA/US/U.S. -> united states) and mirror matches(): exact on the
        # parsed country OR substring of the location text. Country names don't collide with city/
        # state names, so the substring is safe (unlike the city filter).
        from ..extract.geo import country_match_term

        term = country_match_term(q.country)
        cl.append("(LOWER(j.country) = ? OR LOWER(j.location) LIKE ?)")
        p.append(term)
        p.append(f"%{term}%")
    if q.city:
        # Metro-aware exact match (mirrors SearchQuery.matches()._geo_ok via city_match_terms):
        # widens "New York" to its labelled variants ("New York City"/"Brooklyn"/"NYC") so a city
        # filter doesn't miss ~28% of NYC postings, while exact (trimmed) matching avoids the
        # "New York"-the-state / "Brooklyn Park, MN" false positives a substring match would add.
        from ..extract.geo import city_match_terms

        terms = city_match_terms(q.city)
        cl.append("(" + " OR ".join("TRIM(LOWER(j.city)) = ?" for _ in terms) + ")")
        p.extend(terms)
    if q.location:
        # Mirror SearchQuery.matches(): the free-text location must appear in the job's location
        # text (substring). Without this the index ignored `location` and returned non-matching
        # jobs (parity bug vs the live engine).
        cl.append("LOWER(j.location) LIKE ?")
        p.append(f"%{q.location.lower()}%")
    if q.visa_sponsor is True:
        cl.append("j.visa_sponsor = 1")
    if q.sponsorship_offered is not None:
        v = 1 if q.sponsorship_offered else 0
        if q.include_unknown_sponsorship:
            cl.append("(j.sponsorship_offered = ? OR j.sponsorship_offered IS NULL)")
            p.append(v)
        else:
            cl.append("j.sponsorship_offered = ?")
            p.append(v)
    # Salary range overlap, exactly mirroring SearchQuery._salary_ok: a posting with NO salary at
    # all (both bounds NULL) is kept only when include_unknown_salary; a partial range uses the one
    # present bound for both ends (COALESCE), so a min-only posting below the floor is still dropped.
    _sal_unknown = "(j.salary_min IS NULL AND j.salary_max IS NULL)"
    if q.salary_min is not None:
        overlap = "COALESCE(j.salary_max, j.salary_min) >= ?"  # job_hi >= wanted floor
        cl.append(f"({_sal_unknown} OR {overlap})" if q.include_unknown_salary else overlap)
        p.append(q.salary_min)
    if q.salary_max is not None:
        overlap = "COALESCE(j.salary_min, j.salary_max) <= ?"  # job_lo <= wanted ceiling
        cl.append(f"({_sal_unknown} OR {overlap})" if q.include_unknown_salary else overlap)
        p.append(q.salary_max)
    if q.salary_currency and (q.salary_min is not None or q.salary_max is not None):
        # Mirror _salary_ok: when a salary bound is active, drop postings whose currency is set and
        # differs (a USD floor must not return EUR/GBP). NULL-currency postings are kept.
        cl.append("(j.salary_currency IS NULL OR UPPER(j.salary_currency) = ?)")
        p.append(q.salary_currency.upper())
    # Years-of-experience overlap, mirroring _years_ok (same COALESCE/unknown semantics as salary).
    _yr_unknown = "(j.years_min IS NULL AND j.years_max IS NULL)"
    if q.min_years is not None:
        overlap = "COALESCE(j.years_max, j.years_min) >= ?"
        cl.append(f"({_yr_unknown} OR {overlap})" if q.include_unknown_years else overlap)
        p.append(q.min_years)
    if q.max_years is not None:
        overlap = "COALESCE(j.years_min, j.years_max) <= ?"
        cl.append(f"({_yr_unknown} OR {overlap})" if q.include_unknown_years else overlap)
        p.append(q.max_years)
    # Degree ceiling, mirroring _degree_ok: compute the allowed enum values in Python and pass
    # them as an IN-list of placeholders (simpler than a CASE ladder and it uses idx_jobs_degree).
    # Exclusion ignores degree_required on purpose — see SearchQuery.max_degree.
    if q.max_degree is not None:
        allowed = [d for d, rank in DEGREE_ORDER.items() if rank <= DEGREE_ORDER[q.max_degree]]
        in_list = "j.degree_min IN (" + ",".join("?" for _ in allowed) + ")"
        if q.include_unknown_degree:
            cl.append(f"({in_list} OR j.degree_min IS NULL)")
        else:
            cl.append(in_list)
        p.extend(allowed)
    if q.employment_type is not None:
        # Mirror matches(): keep the requested type plus UNKNOWN (most postings don't state it).
        cl.append("(j.employment_type = ? OR j.employment_type = 'unknown')")
        p.append(q.employment_type.value)
    if q.posted_after is not None:
        # Mirror matches(): drop postings older than the cutoff; keep those with no posted_at
        # (unknown date). posted_at is stored as an ISO-8601 string, which sorts chronologically.
        cl.append("(j.posted_at IS NULL OR j.posted_at >= ?)")
        p.append(q.posted_after.isoformat())
    if q.max_age_days is not None:
        # Freshness floor: keep postings whose most-recent activity (max of posted_at/updated_at,
        # ISO strings that sort chronologically) is within max_age_days. NULL dates -> '' (sorts
        # oldest), so undated rows are dropped unless include_undated. This hides the filled-but-
        # never-closed stale tail (a posting's presence on a board is not proof it's active).
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=q.max_age_days)).isoformat()
        fresh = "MAX(COALESCE(j.posted_at, ''), COALESCE(j.updated_at, ''))"
        if q.include_undated:
            cl.append(f"({fresh} >= ? OR {fresh} = '')")
        else:
            cl.append(f"{fresh} >= ?")
        p.append(cutoff)
    return cl, p


def search_rows(con: sqlite3.Connection, q: SearchQuery) -> list[sqlite3.Row]:
    where, params = _where(q)
    limit = q.limit or 1000
    # Branch on the *expanded* match expr: keywords with no alphanumeric tokens (e.g. '"""'
    # or pure punctuation) yield "" — taking the FTS path then would `MATCH ''` and raise an
    # FTS5 syntax error, so fall through to the filter-only path (no keyword constraint).
    match = _match_expr(q.keywords) if q.keywords else ""
    if match:
        sql = (
            "SELECT j.* FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid "
            "WHERE jobs_fts MATCH ? AND "
            + " AND ".join(where)
            + " ORDER BY bm25(jobs_fts, 10,3,3,1)"
            " LIMIT ?"
        )
        return con.execute(sql, [match, *params, limit]).fetchall()
    sql = (
        "SELECT j.* FROM jobs j WHERE " + " AND ".join(where) + " ORDER BY j.posted_at DESC LIMIT ?"
    )
    return con.execute(sql, [*params, limit]).fetchall()


def whats_new_rows(
    con: sqlite3.Connection, q: SearchQuery, since_iso: str, *, include_changed: bool = False
) -> list[sqlite3.Row]:
    """The 'what's new' feed: index rows first seen (or, with ``include_changed``, first-seen-or-updated)
    on/after ``since_iso``, newest first.

    The prebuilt index stamps ``first_seen``/``updated_at`` per job, so it answers "what appeared since
    X" with **zero ATS calls**. Reuses the standard filter set (``_where`` already constrains
    ``status='active'``), so every search filter (keywords, location, level, sector, salary, visa, …)
    composes with the recency cutoff. No competitor MCP exposes a real diff feed; this is ours."""
    where, params = _where(q)
    if include_changed:
        where = [*where, "(j.first_seen >= ? OR j.updated_at >= ?)"]
        params = [*params, since_iso, since_iso]
    else:
        where = [*where, "j.first_seen >= ?"]
        params = [*params, since_iso]
    limit = q.limit or 100
    order = "ORDER BY j.first_seen DESC, j.posted_at DESC"
    match = _match_expr(q.keywords) if q.keywords else ""
    if match:
        sql = (
            "SELECT j.* FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid "
            "WHERE jobs_fts MATCH ? AND " + " AND ".join(where) + f" {order} LIMIT ?"
        )
        return con.execute(sql, [match, *params, limit]).fetchall()
    sql = "SELECT j.* FROM jobs j WHERE " + " AND ".join(where) + f" {order} LIMIT ?"
    return con.execute(sql, [*params, limit]).fetchall()
