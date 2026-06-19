"""ergon_tracker MCP server — exposes the SDK as Model Context Protocol tools.

Run it (after ``pip install 'ergon-tracker[mcp]'``)::

    ergon-tracker-mcp            # stdio transport (for Claude Desktop / MCP clients)

Tools:
- ``search_jobs``     — unified search across ATS feeds + aggregators
- ``resolve_company`` — detect which ATS a company/URL uses + its board token
- ``list_sources``    — registered providers + registry size

The tools are thin adapters over the existing ``ergon_tracker`` API. ``search_jobs`` returns a
compact view of each posting (no raw payload / HTML) to keep tool responses small.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import AsyncErgonTracker
from .engine import AGGREGATOR_PROVIDERS
from .models import EmploymentType, JobLevel, JobPosting, SearchQuery
from .providers.base import iter_providers, load_builtins, load_plugins
from .registry.resolver import resolve
from .registry.store import SeedRegistry

mcp = FastMCP("ergon-tracker")


def _days_ago(days: int | None) -> datetime | None:
    """Cutoff datetime for `posted_within_days` (None -> no recency filter)."""
    if not days or days <= 0:
        return None
    from datetime import timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(days=days)


def _job_to_dict(job: JobPosting) -> dict[str, Any]:
    salary: dict[str, Any] | None = None
    if job.salary and (job.salary.min_amount or job.salary.max_amount):
        salary = {
            "min": job.salary.min_amount,
            "max": job.salary.max_amount,
            "currency": job.salary.currency,
            "interval": job.salary.interval.value if job.salary.interval else None,
        }
    return {
        "company": job.company,
        "title": job.title,
        "location": job.locations[0].as_text() if job.locations else None,
        "remote": job.remote.value,
        "level": job.level.value,
        "sector": job.sector,
        "employment_type": job.employment_type.value,
        "salary": salary,
        "apply_url": job.apply_url,
        "source": job.source,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "found_on": [p.source for p in job.provenance],
        "score": round(job.score, 4) if job.score is not None else None,
        "visa_sponsor": job.visa_sponsor,
        "visa_last_filed": job.visa_last_filed,
        "sponsorship_offered": job.sponsorship_offered,
    }


@mcp.tool()
async def search_jobs(
    keywords: str | None = None,
    location: str | None = None,
    remote: bool | None = None,
    companies: list[str] | None = None,
    sources: list[str] | None = None,
    level: str | None = None,
    include_unknown_level: bool = False,
    sector: str | None = None,
    include_unknown_sector: bool = False,
    country: str | None = None,
    city: str | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
    salary_currency: str | None = None,
    min_years: int | None = None,
    max_years: int | None = None,
    include_unknown_years: bool = True,
    employment_type: str | None = None,
    posted_within_days: int | None = None,
    visa_sponsor: bool = False,
    sponsorship_offered: bool | None = None,
    infer_level_from_experience: bool = False,
    semantic: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Search jobs across company ATS feeds and aggregators, returning canonical postings.

    Args:
        keywords: free-text query. Matches on title/department/company/description, then
            results are ranked by relevance (field-weighted BM25; title matches rank highest)
            so the most relevant postings come first. Each job carries a `score`.
        semantic: if true, rank by meaning via embeddings instead of exact-token matching
            (handles synonyms / natural-language intent). Needs the server's `semantic` extra.
        location: substring match on posting location.
        remote: if true, keep only remote/hybrid roles.
        companies: company domains or careers URLs to target (e.g. ["stripe.com", "ramp.com"]).
            Best for "roles at <company>" questions — fast and precise.
        sources: restrict to provider names (e.g. greenhouse, lever, remoteok, adzuna). For a
            BROAD keyword search, leave both `companies` and `sources` empty: this tool then
            queries only the fast single-call aggregator/keyed APIs (RemoteOK, Remotive, Adzuna,
            USAJOBS, …) — quick and rate-limit-friendly. (A full 42k-company ATS crawl only
            happens if you explicitly pass ATS provider names in `sources`.)
        level: seniority filter — intern/entry/junior/mid/senior/staff/principal/lead/manager/
            director/executive (inferred from title; many titles have none -> "unknown").
        include_unknown_level: keep postings whose level couldn't be inferred (narrow without
            dropping unlabeled roles). Recommended when filtering by level on real data.
        sector: industry filter, e.g. "Fintech", "AI/ML", "Healthcare" (NAICS-informed).
        include_unknown_sector: keep postings with no detected sector.
        country / city: structured location filter. country accepts common aliases (USA/US ->
            United States, UK -> United Kingdom); city is metro-aware ("New York" also matches
            NYC boroughs / "New York City" / "NYC").
        salary_min / salary_max: compensation range (jobs without salary data are kept).
        salary_currency: restrict to a currency (e.g. "USD") when a salary bound is set, so a
            $140k floor doesn't return EUR/GBP postings.
        min_years / max_years: required years-of-experience range — ideal for "new grad / 0-2
            years" searches (min_years=0, max_years=2). Postings with no stated years are kept
            unless include_unknown_years=false.
        include_unknown_years: keep postings with no stated experience requirement (default true).
        employment_type: full_time / part_time / contract / internship / temporary / other.
            Postings that don't state a type are kept.
        posted_within_days: only postings published within the last N days (recency filter).
        visa_sponsor: if true, keep only employers known to have sponsored H-1B visas (from US
            DoL LCA certified-filing data). Each job also reports a `visa_sponsor` flag.
        sponsorship_offered: filter on what the POSTING says about visa sponsorship. true =
            postings that offer it; false = postings that explicitly don't. Unknown postings
            (the majority) are kept by default. Each job reports `sponsorship_offered`
            (true/false/null). Tip for international applicants: pass false-exclusion by using
            true here to hide "no sponsorship" roles while keeping unstated ones.
        infer_level_from_experience: when a title has no seniority word, derive level from the
            required years of experience (boosts level coverage; combine with a `level` filter).
        semantic: rank by meaning via embeddings instead of exact-token matching (handles
            synonyms / natural-language intent). Needs the server's `semantic` extra.
        limit: max postings to return after dedup + ranking (default 20).

    Returns a dict with `count`, `jobs` (compact, relevance-ranked, each with a `score`),
    and per-source `health`.

    Examples (combine filters freely):
        # roles at specific companies
        search_jobs(keywords="engineer", companies=["stripe.com", "ramp.com"], level="senior")
        # broad, fast (auto aggregator APIs), well-paid + remote
        search_jobs(keywords="data scientist", remote=True, country="United States", salary_min=150000)
        # industry + level, keeping roles whose level/sector couldn't be inferred
        search_jobs(keywords="backend", sector="Fintech", level="senior",
                    include_unknown_level=True, include_unknown_sector=True)
        # international applicant: known H-1B sponsors AND posting doesn't refuse sponsorship
        search_jobs(keywords="ml engineer", visa_sponsor=True, sponsorship_offered=True, semantic=True)
        # derive level from required experience when titles omit it
        search_jobs(keywords="developer", level="senior", infer_level_from_experience=True)
        # restrict to specific sources (incl. ATS names = deliberate, slower crawl)
        search_jobs(keywords="rust", sources=["greenhouse", "lever", "ashby"])
    """
    query = SearchQuery(
        keywords=keywords,
        location=location,
        remote=remote,
        companies=companies,
        sources=sources,
        level=JobLevel(level) if level else None,
        include_unknown_level=include_unknown_level,
        sector=sector,
        include_unknown_sector=include_unknown_sector,
        country=country,
        city=city,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        min_years=min_years,
        max_years=max_years,
        include_unknown_years=include_unknown_years,
        employment_type=EmploymentType(employment_type) if employment_type else None,
        posted_after=_days_ago(posted_within_days),
        visa_sponsor=True if visa_sponsor else None,
        sponsorship_offered=sponsorship_offered,
        infer_level_from_experience=infer_level_from_experience,
        semantic=semantic,
        limit=limit,
    )
    # Broad search (no companies, no sources): serve from the prebuilt INDEX — fast, throttle-proof,
    # and covering ALL ATSes we track. Only if the index is unavailable do we fall back to the fast
    # aggregator/keyed APIs (NEVER a live fan-out across the whole ~46k-board registry, which would
    # be slow + rate-limit-prone for an interactive agent). Targeted queries skip this entirely.
    if not companies and not sources:
        from .index.router import try_index

        indexed = try_index(query)
        if indexed is not None:
            return {
                "count": len(indexed),
                "jobs": [_job_to_dict(j) for j in indexed],
                "health": [{"source": "index", "ok": True, "count": len(indexed), "error": None}],
            }
        query = query.model_copy(update={"sources": list(AGGREGATOR_PROVIDERS)})  # index down

    async with AsyncErgonTracker() as js:
        result = await js.search(query)
    return {
        "count": len(result.jobs),
        "jobs": [_job_to_dict(j) for j in result.jobs],
        "health": [h.model_dump() for h in result.health],
    }


@mcp.tool()
def list_h1b_sponsors(query: str | None = None, limit: int = 25) -> dict[str, Any]:
    """Browse employers known to sponsor H-1B visas (US DoL LCA certified filings).

    Use this to answer "does <company> sponsor H-1B?" or "show the biggest H-1B sponsors in
    fintech/<name>". Returns sponsors ranked by filing volume, each with the most-recent filing
    date (so you can judge whether they've gone quiet). This is sponsor *knowledge* — it covers
    far more employers than we can fetch live jobs for, so it's useful even for big companies
    (Microsoft/Google/Amazon) whose jobs live on custom career sites.

    Args:
        query: filter by employer name (blank/None = the largest sponsors overall).
        limit: max rows (default 25).
    """
    from .extract.visa import search_sponsors

    rows = search_sponsors(query, limit)
    return {"count": len(rows), "sponsors": rows}


@mcp.tool()
def resolve_company(target: str) -> dict[str, Any]:
    """Detect which ATS a company uses and its board token, from a domain or careers URL.

    Example: resolve_company("stripe.com") -> {ats: "greenhouse", token: "stripe", ...}
    """
    res = resolve(target)
    return {
        "ats": res.ats,
        "token": res.token,
        "domain": res.domain,
        "matched": res.matched,
        "query": res.source,
    }


@mcp.tool()
def list_sources() -> dict[str, Any]:
    """List registered providers and the number of companies in the bundled registry."""
    load_builtins()
    load_plugins()
    return {
        "providers": sorted(p.name for p in iter_providers()),
        "registry_companies": len(SeedRegistry()),
    }


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
