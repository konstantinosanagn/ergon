"""Aggregate canonical JobPostings into canonical Company entities (reuses dedup keys)."""

from __future__ import annotations

from .dedup import normalize_company
from .extract.visa import h1b_last_filed, is_h1b_sponsor
from .models import Company, JobPosting

__all__ = ["aggregate_companies"]


def aggregate_companies(jobs: list[JobPosting]) -> list[Company]:
    """Group postings into Company rows keyed by the same normalize_company live dedup uses."""
    out: dict[str, Company] = {}
    for j in jobs:
        key = normalize_company(j.company)
        if not key:
            continue
        c = out.get(key)
        if c is None:
            out[key] = Company(
                company_key=key,
                display_name=j.company,
                domain=j.company_domain,
                primary_ats=j.source,
                sector=j.sector,
                h1b_sponsor=True if is_h1b_sponsor(j.company) else None,
                h1b_last_filed=h1b_last_filed(j.company),
                open_roles=1,
            )
        else:
            c.open_roles += 1
            if not c.domain and j.company_domain:
                c.domain = j.company_domain
            if not c.sector and j.sector:
                c.sector = j.sector
    return list(out.values())
