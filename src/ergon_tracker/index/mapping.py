"""The single JobPosting <-> SQLite row mapping (build + read share it)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from ..dedup import normalize_company, normalize_title
from ..models import (
    EmploymentType,
    JobLevel,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SalaryInterval,
)

_SNIPPET = 300


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s: Any) -> datetime | None:
    """Parse a stored ISO datetime string back to a datetime (None/blank/garbage -> None)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def content_hash(job: JobPosting) -> str:
    """Stable hash of the fields that define a posting's content (for change/delta detection).

    Independent of the source id: two crawls of an unchanged posting hash identically; a changed
    title/location/salary changes the hash, so the incremental builder can tell what moved.
    """
    loc = job.locations[0] if job.locations else None
    loc_s = (loc.as_text() if loc else "").lower()
    s = job.salary
    sal_s = f"{s.min_amount}|{s.max_amount}|{s.currency}" if s else ""
    basis = f"{normalize_company(job.company)}|{normalize_title(job.title)}|{loc_s}|{sal_s}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def to_row(job: JobPosting, *, build_id: str, now: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc).date().isoformat()
    loc = job.locations[0] if job.locations else None
    s = job.salary
    desc = job.description_text or ""
    return {
        "id": job.id,
        "content_hash": content_hash(job),
        "company_key": normalize_company(job.company),
        "source": job.source,
        "company": job.company,
        "company_domain": job.company_domain,
        "title": job.title,
        "department": job.department,
        "role_family": normalize_title(job.title),
        "location": loc.as_text() if loc else None,
        "city": loc.city if loc else None,
        "country": loc.country if loc else None,
        "remote": job.remote.value,
        "level": job.level.value,
        "employment_type": job.employment_type.value,
        "sector": job.sector,
        "salary_min": s.min_amount if s else None,
        "salary_max": s.max_amount if s else None,
        "salary_currency": s.currency if s else None,
        "salary_interval": s.interval.value if s and s.interval else None,
        "salary_annual": None,
        "years_min": job.years_experience_min,
        "years_max": job.years_experience_max,
        "visa_sponsor": 1 if job.visa_sponsor else None,
        "visa_last_filed": job.visa_last_filed,
        "sponsorship_offered": (
            None if job.sponsorship_offered is None else (1 if job.sponsorship_offered else 0)
        ),
        "apply_url": job.apply_url,
        "listing_url": job.apply_url,
        "board_token": None,
        "posted_at": _iso(job.posted_at),
        "updated_at": _iso(job.updated_at),
        "closes_at": None,
        "status": "active",
        "first_seen": now,
        "last_seen": now,
        "expired_at": None,
        "expiry_reason": None,
        "fetched_at": now,
        "build_id": build_id,
        "snippet": desc[:_SNIPPET] or None,
    }


def from_row(row: Any) -> JobPosting:
    sal = None
    if row["salary_min"] is not None or row["salary_max"] is not None:
        sal = Salary(
            min_amount=row["salary_min"],
            max_amount=row["salary_max"],
            currency=row["salary_currency"],
            interval=SalaryInterval(row["salary_interval"]) if row["salary_interval"] else None,
        )
    locs = []
    if row["location"] or row["city"] or row["country"]:
        locs = [
            Location(
                city=row["city"],
                country=row["country"],
                raw=row["location"],
                is_remote=row["remote"] == "remote",
            )
        ]
    sp = row["sponsorship_offered"]
    return JobPosting(
        id=row["id"],
        source=row["source"],
        source_job_id=row["id"],
        company=row["company"],
        company_domain=row["company_domain"],
        title=row["title"],
        description_text=row["snippet"],
        department=row["department"],
        sector=row["sector"],
        locations=locs,
        remote=RemoteType(row["remote"]),
        level=JobLevel(row["level"]),
        employment_type=EmploymentType(row["employment_type"]),
        salary=sal,
        years_experience_min=row["years_min"],
        years_experience_max=row["years_max"],
        apply_url=row["apply_url"],
        posted_at=_parse_dt(row["posted_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        visa_sponsor=True if row["visa_sponsor"] == 1 else None,
        visa_last_filed=row["visa_last_filed"],
        sponsorship_offered=(None if sp is None else bool(sp)),
    )
