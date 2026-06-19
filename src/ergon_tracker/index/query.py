"""Translate a SearchQuery into SQL over the index, mirroring SearchQuery.matches() semantics."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..models import SearchQuery

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
    if q.salary_min is not None:
        cl.append(
            "(j.salary_max IS NULL OR j.salary_max >= ?)"
            if q.include_unknown_salary
            else "j.salary_max >= ?"
        )
        p.append(q.salary_min)
    if q.salary_max is not None:
        cl.append(
            "(j.salary_min IS NULL OR j.salary_min <= ?)"
            if q.include_unknown_salary
            else "j.salary_min <= ?"
        )
        p.append(q.salary_max)
    if q.salary_currency and (q.salary_min is not None or q.salary_max is not None):
        # Mirror SearchQuery._salary_ok: when a salary bound is active, drop postings whose currency
        # is set and differs (a USD floor must not return EUR/GBP). NULL-currency postings are kept
        # (currency unknown), exactly as matches() does.
        cl.append("(j.salary_currency IS NULL OR UPPER(j.salary_currency) = ?)")
        p.append(q.salary_currency.upper())
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
