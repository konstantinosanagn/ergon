"""Canonical JSON serialization for a :class:`JobPosting` — shared by the MCP server and the HTTP
QUERY surface so both return an identical wire shape (one source of truth, no drift)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import JobPosting


def job_to_dict(job: JobPosting) -> dict[str, Any]:
    """Serialize a posting to the public API/tool shape (stable field set + ordering)."""
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
        "years_min": job.years_experience_min,
        "years_max": job.years_experience_max,
        "degree_min": job.degree_min,
        "degree_required": job.degree_required,
        "apply_url": job.apply_url,
        "source": job.source,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "found_on": [p.source for p in job.provenance],
        "score": round(job.score, 4) if job.score is not None else None,
        "visa_sponsor": job.visa_sponsor,
        "visa_last_filed": job.visa_last_filed,
        "sponsorship_offered": job.sponsorship_offered,
    }
