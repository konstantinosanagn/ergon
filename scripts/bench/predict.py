"""Extractor runner: reconstruct a JobPosting from a corpus row, run the REAL enrichment
pipeline, and read back the extractor's value for every field in ``scripts.bench.schema.FIELDS``.

Output vocabulary mirrors the fleet/human rubric in ``docs/extraction-labeling-guide.md``:
level -> str (JobLevel.value, e.g. "senior"), sector -> str|None (title-case label, e.g.
"Software/SaaS"), country/city -> str|None, remote -> bool, employment_type -> str
(EmploymentType.value), salary -> {"min","max","currency","interval"}|None, yoe ->
{"min","max"}|None, degree -> str|None (DEGREE_LEVELS member, e.g. "bachelor"), sponsorship ->
bool|None, posted_at -> ISO string|None, visa_sponsor -> bool|None.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.enrich import enrich_in_place
from ergon_tracker.models import JobPosting, Location, RemoteType, Salary

from .schema import FIELDS

__all__ = ["predict"]


def _structured_salary_from_row(raw: Any) -> Salary | None:
    """A corpus row's ``structured_salary`` (``{"min","max","currency","interval"}|None``) as a
    ``Salary``, so the comp extractor's structured arm can use it (fed onto ``JobPosting.salary``
    BEFORE enrichment; ``enrich_in_place`` never overwrites an already-set field)."""
    if not raw:
        return None
    return Salary(
        min_amount=raw.get("min"),
        max_amount=raw.get("max"),
        currency=raw.get("currency"),
        interval=raw.get("interval"),
    )


def _salary_to_dict(salary: Salary | None) -> dict[str, Any] | None:
    if salary is None or (salary.min_amount is None and salary.max_amount is None):
        return None
    return {
        "min": salary.min_amount,
        "max": salary.max_amount,
        "currency": salary.currency,
        "interval": salary.interval.value if salary.interval else None,
    }


def _yoe_to_dict(lo: int | None, hi: int | None) -> dict[str, Any] | None:
    if lo is None and hi is None:
        return None
    return {"min": lo, "max": hi}


def predict(row: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct a ``JobPosting`` from a corpus ``row``, run the real ``enrich_in_place``, and
    return a flat dict with one entry per ``FIELDS``, normalized to the fleet-label vocabulary."""
    location = Location(raw=row.get("location_raw") or None)
    job = JobPosting.create(
        source=row.get("source") or "bench",
        source_job_id=row.get("id") or "bench",
        company=row.get("company") or "",
        title=row.get("title") or "",
        description_text=row.get("description_text") or None,
        locations=[location],
        salary=_structured_salary_from_row(row.get("structured_salary")),
    )
    # Mirror production (engine.py's worker): the sector extractor's Stage-1 gazetteer lookup is
    # keyed by the registry token (company_key) and, as a fallback, the company's domain. Without
    # these the corpus only exercises the weak name/brand fallbacks. company_domain lives directly
    # on JobPosting (set before enrichment, same as engine.py does for target.domain); company_key
    # is passed into enrich_in_place, same as engine.py's `company_key=target.label`.
    if row.get("company_domain") and not job.company_domain:
        job.company_domain = row["company_domain"]
    enrich_in_place(job, company_key=row.get("company_key"))

    loc = job.locations[0] if job.locations else Location()
    is_remote = job.remote in (RemoteType.REMOTE, RemoteType.HYBRID) or loc.is_remote

    out: dict[str, Any] = {
        "level": job.level.value,
        "sector": job.sector,
        "country": loc.country,
        "city": loc.city,
        "remote": is_remote,
        "employment_type": job.employment_type.value,
        "salary": _salary_to_dict(job.salary),
        "yoe": _yoe_to_dict(job.years_experience_min, job.years_experience_max),
        "degree": job.degree_min,
        "sponsorship": job.sponsorship_offered,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "visa_sponsor": job.visa_sponsor,
    }
    # Direct-ATS-payload fields are NOT inferred by any extractor — the "prediction" is the value the
    # provider gave, which the corpus row carries (see crawl_corpus.row_from_job). Reflect those
    # provider-stated values so employment_type / posted_at / remote are actually measurable (else
    # they always read as "extractor asserted nothing"). Absent keys leave the enrich-derived value.
    if row.get("employment_type"):
        out["employment_type"] = row["employment_type"]
    if row.get("posted_at"):
        out["posted_at"] = row["posted_at"]
    if row.get("remote") not in (None, ""):
        out["remote"] = str(row["remote"]).strip().lower() in ("remote", "hybrid", "true", "1")

    assert set(out) == set(FIELDS)
    return out
