"""jobspine MCP server — exposes the SDK as Model Context Protocol tools.

Run it (after ``pip install 'jobspine[mcp]'``)::

    jobspine-mcp            # stdio transport (for Claude Desktop / MCP clients)

Tools:
- ``search_jobs``     — unified search across ATS feeds + aggregators
- ``resolve_company`` — detect which ATS a company/URL uses + its board token
- ``list_sources``    — registered providers + registry size

The tools are thin adapters over the existing ``jobspine`` API. ``search_jobs`` returns a
compact view of each posting (no raw payload / HTML) to keep tool responses small.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import AsyncJobSpine
from .models import JobLevel, JobPosting, SearchQuery
from .providers.base import iter_providers, load_builtins, load_plugins
from .registry.resolver import resolve
from .registry.store import SeedRegistry

mcp = FastMCP("jobspine")


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
    }


@mcp.tool()
async def search_jobs(
    keywords: str | None = None,
    location: str | None = None,
    remote: bool | None = None,
    companies: list[str] | None = None,
    sources: list[str] | None = None,
    level: str | None = None,
    sector: str | None = None,
    country: str | None = None,
    city: str | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search jobs across company ATS feeds and aggregators, returning canonical postings.

    Args:
        keywords: free-text match on title/department/company/description.
        location: substring match on posting location.
        remote: if true, keep only remote/hybrid roles.
        companies: company domains or careers URLs to target (e.g. ["stripe.com"]). Omit to
            search the entire bundled registry (broader but slower).
        sources: restrict to provider names: greenhouse, lever, ashby, workday, remoteok.
        level: seniority filter — intern/entry/junior/mid/senior/staff/principal/lead/manager/
            director/executive (inferred from title).
        sector: industry filter, e.g. "Fintech", "AI/ML", "Healthcare" (NAICS-informed).
        country / city: structured location filter.
        salary_min / salary_max: compensation range (jobs without salary data are kept).
        limit: max postings to return after dedup (default 20).

    Returns a dict with `count`, `jobs` (compact), and per-source `health`.
    """
    query = SearchQuery(
        keywords=keywords,
        location=location,
        remote=remote,
        companies=companies,
        sources=sources,
        level=JobLevel(level) if level else None,
        sector=sector,
        country=country,
        city=city,
        salary_min=salary_min,
        salary_max=salary_max,
        limit=limit,
    )
    async with AsyncJobSpine() as js:
        result = await js.search(query)
    return {
        "count": len(result.jobs),
        "jobs": [_job_to_dict(j) for j in result.jobs],
        "health": [h.model_dump() for h in result.health],
    }


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
