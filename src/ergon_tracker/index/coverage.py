"""Coverage report: what's actually inside a built index, for users and forkers.

`compute_coverage(con)` reduces the index to a structured summary (totals, provider/sector/
country/level breakdowns, top companies, salary/visa coverage). `render_status_md(...)` turns
that into a human-readable INDEX_STATUS.md. The build publishes both alongside the index so
anyone can see coverage across all ATSes without downloading and querying the database.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..dedup import normalize_company


def company_directory(
    con: sqlite3.Connection,
    registry: dict[str, dict[str, Any]],
    *,
    status: str | None = None,
    query: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Join the registry (every known board = the universe) with the index's per-company
    ``open_roles`` to label each registered company **active** (>=1 live posting) or **dormant**
    (registered, no current postings), and report how many postings each has.

    The join is on ``normalize_company`` of both sides: the registry key is a slug
    (``palantir-technologies``) while the index ``company_key`` is the normalized company name
    (``palantir technologies``); ``board_token`` in the index is not the registry token, so it
    can't be used. Status is *derived per build* (never a stale stored flag) — a company that
    starts posting flips to active on the next build automatically.

    ``status`` filters the returned list ('active'/'dormant'/None=all); ``query`` is a
    case-insensitive substring on the company key; ``limit`` caps the list (counts are always the
    full totals). Companies with postings that aren't in the registry (e.g. aggregator-only) are
    reported as ``index_only``.
    """
    roles: dict[str, int] = {}
    for company_key, open_roles in con.execute("SELECT company_key, open_roles FROM companies"):
        n = normalize_company(company_key)
        roles[n] = roles.get(n, 0) + int(open_roles or 0)

    matched: set[str] = set()
    companies: list[dict[str, Any]] = []
    active = 0
    for key, entry in registry.items():
        n = normalize_company(key)
        matched.add(n)
        open_roles = roles.get(n, 0)
        is_active = open_roles > 0
        active += int(is_active)
        companies.append(
            {
                "company": key,
                "ats": entry.get("ats"),
                "domain": entry.get("domain"),
                "status": "active" if is_active else "dormant",
                "open_roles": open_roles,
            }
        )
    index_only = sum(1 for n, r in roles.items() if r > 0 and n not in matched)

    rows = companies
    if status in ("active", "dormant"):
        rows = [c for c in rows if c["status"] == status]
    if query:
        q = query.lower()
        rows = [c for c in rows if q in str(c["company"]).lower()]
    rows = sorted(rows, key=lambda c: (-c["open_roles"], c["company"]))
    if limit is not None:
        rows = rows[:limit]

    return {
        "registered": len(registry),
        "active": active,
        "dormant": len(registry) - active,
        "index_only": index_only,
        "companies": rows,
    }


def _counts(con: sqlite3.Connection, col: str, *, limit: int | None = None) -> dict[str, int]:
    """`{value: count}` for a column over active jobs, NULLs skipped, sorted by count desc."""
    sql = (
        f"SELECT {col}, COUNT(*) c FROM jobs "  # noqa: S608 - col is a fixed internal identifier
        f"WHERE {col} IS NOT NULL AND {col} != '' AND status='active' "
        f"GROUP BY {col} ORDER BY c DESC, {col} ASC"
    )
    rows = con.execute(sql).fetchall()
    if limit is not None:
        rows = rows[:limit]
    return {r[0]: r[1] for r in rows}


def compute_coverage(con: sqlite3.Connection) -> dict[str, Any]:
    """Reduce an index connection to a JSON-serializable coverage summary."""

    def one(q: str) -> int:
        return int(con.execute(q).fetchone()[0])

    total = one("SELECT COUNT(*) FROM jobs")
    active = one("SELECT COUNT(*) FROM jobs WHERE status='active'")
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    top = con.execute(
        "SELECT company, COUNT(*) c FROM jobs WHERE status='active' "
        "GROUP BY company ORDER BY c DESC, company ASC LIMIT 20"
    ).fetchall()
    return {
        "total_jobs": total,
        "active_jobs": active,
        "expired_jobs": total - active,
        "companies": one("SELECT COUNT(*) FROM companies"),
        "by_source": _counts(con, "source"),
        "by_sector": _counts(con, "sector"),
        "by_level": _counts(con, "level"),
        "by_country": _counts(con, "country", limit=20),
        "remote": _counts(con, "remote"),
        "with_salary": one(
            "SELECT COUNT(*) FROM jobs WHERE (salary_min IS NOT NULL OR salary_max IS NOT NULL) "
            "AND status='active'"
        ),
        # JD-text capture: the #1 data lever (salary/yoe/degree extraction is near-zero without JD
        # text). A single cheap COUNT of active rows that actually carry a non-empty snippet, so the
        # capture % is a first-class published number instead of a one-off script measurement.
        "with_jd": one(
            "SELECT COUNT(*) FROM jobs WHERE snippet IS NOT NULL AND TRIM(snippet) != '' "
            "AND status='active'"
        ),
        "visa_sponsors": one("SELECT COUNT(*) FROM jobs WHERE visa_sponsor=1 AND status='active'"),
        "sponsorship_offered": one(
            "SELECT COUNT(*) FROM jobs WHERE sponsorship_offered=1 AND status='active'"
        ),
        "top_companies": [{"company": c, "jobs": n} for c, n in top],
        "build_id": meta.get("build_id", ""),
    }


def _table(rows: list[tuple[str, int]], headers: tuple[str, str]) -> str:
    if not rows:
        return "_none_\n"
    out = [f"| {headers[0]} | {headers[1]} |", "| --- | ---: |"]
    out += [f"| {k} | {v:,} |" for k, v in rows]
    return "\n".join(out) + "\n"


def render_status_md(cov: dict[str, Any], *, build_id: str) -> str:
    """Render a coverage dict to a human-readable INDEX_STATUS.md body."""
    active = cov["active_jobs"]
    pct = (cov["with_salary"] / active * 100) if active else 0.0
    jd_pct = (cov.get("with_jd", 0) / active * 100) if active else 0.0
    parts = [
        "# Index Status\n",
        f"Build `{build_id}` — **{cov['total_jobs']:,}** jobs "
        f"({cov['active_jobs']:,} active, {cov['expired_jobs']:,} expired) "
        f"across **{cov['companies']:,}** companies.\n",
        f"- JD text captured: {cov.get('with_jd', 0):,} ({jd_pct:.0f}% of active)",
        f"- Salary disclosed: {cov['with_salary']:,} ({pct:.0f}% of active)",
        f"- Visa-sponsor history: {cov['visa_sponsors']:,}",
        f"- Sponsorship offered: {cov['sponsorship_offered']:,}\n",
        "## By provider (ATS)\n",
        _table(list(cov["by_source"].items()), ("Provider", "Jobs")),
        "## By sector\n",
        _table(list(cov["by_sector"].items()), ("Sector", "Jobs")),
        "## By level\n",
        _table(list(cov["by_level"].items()), ("Level", "Jobs")),
        "## Top countries\n",
        _table(list(cov["by_country"].items()), ("Country", "Jobs")),
        "## Top companies\n",
        _table([(c["company"], c["jobs"]) for c in cov["top_companies"]], ("Company", "Jobs")),
    ]
    return "\n".join(parts)
