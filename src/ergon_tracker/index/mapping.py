"""The single JobPosting <-> SQLite row mapping (build + read share it)."""

from __future__ import annotations

import hashlib
import re
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
    title/level/location/salary changes the hash, so the incremental builder can tell what moved.
    ``level`` is part of the identity because ``normalize_title`` strips seniority — without it a
    "Senior X" and a plain "X" in the same city would collide to one hash (mirrors dedup's
    level gate, so distinct seniorities stay distinct rows in the delta stream too).
    """
    loc = job.locations[0] if job.locations else None
    loc_s = (loc.as_text() if loc else "").lower()
    s = job.salary
    sal_s = f"{s.min_amount}|{s.max_amount}|{s.currency}" if s else ""
    basis = (
        f"{normalize_company(job.company)}|{normalize_title(job.title)}"
        f"|{job.level.value}|{loc_s}|{sal_s}"
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _normalize_description(job: JobPosting) -> str:
    """Normalize the JD body to the text enrichment actually reads.

    Prefers ``description_text``; falls back to a tag-stripped ``description_html`` when only
    that's populated (mirrors how enrichment itself falls back — see ``enrich.py``: "Uses
    inp.description_text, which falls back to stripped description_html for aggregators").
    Collapses whitespace/casing so re-wrapped or re-indented markup that carries the *same*
    words hashes the same — only wording changes should flip ``enrich_hash``.
    """
    text = job.description_text
    if not text and job.description_html:
        text = _TAG_RE.sub(" ", job.description_html)
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _snippet_source(job: JobPosting) -> str:
    """Display text for the stored ``snippet``: prefer ``description_text``; fall back to the
    tag-stripped, whitespace-collapsed ``description_html`` (case preserved, unlike the hash-only
    ``_normalize_description``). Enrichment already reads the JD via this same html fallback
    (see ``enrich.py`` / ``_normalize_description``), but the snippet historically did not -- so an
    html-only provider (jazzhr + ~15 others: adzuna/dayforce/dejobs/themuse/ukg/workable_network/...)
    captured the full JD yet still counted as "no JD" and got queued for a needless Tier-3 detail
    fetch. Falling back here makes the snippet (and thus the with_jd metric + FTS text) reflect the
    JD that was already captured, at zero network cost."""
    text = job.description_text
    if not text and job.description_html:
        text = _WS_RE.sub(" ", _TAG_RE.sub(" ", job.description_html)).strip()
    return text or ""


def enrich_hash(job: JobPosting) -> str:
    """Body-inclusive fingerprint that makes enrich-reuse SAFE (delta-driven crawl redesign, Phase 3).

    ``content_hash`` deliberately excludes the JD body (company/title/level/location/salary
    only), but ``enrich_in_place`` extracts salary/years/degree/sector/sponsorship FROM the JD
    body. A cache keyed only on ``content_hash`` would silently reuse a stale enriched row for a
    posting whose title/salary/location are unchanged but whose JD text was rewritten. Folding
    ``content_hash`` plus the normalized body into one hash fixes that: it changes whenever the
    JD body changes (even with everything else identical), and stays stable across insignificant
    whitespace/markup-only edits (normalization above), so genuinely unchanged postings still
    hit the reuse path.
    """
    basis = f"{content_hash(job)}|{_normalize_description(job)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def to_row(job: JobPosting, *, build_id: str, now: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc).date().isoformat()
    loc = job.locations[0] if job.locations else None
    s = job.salary
    desc = _snippet_source(job)
    return {
        "id": job.id,
        "content_hash": content_hash(job),
        # Persist the PRE-enrich fingerprint the crawler stamped before enrich_in_place mutated
        # level/salary (see JobPosting._enrich_input_hash). enrich_in_place feeds content_hash, so a
        # post-enrich hash (the fallback, for any unstamped path) would never match the pre-enrich
        # hash the reuse path computes -> a level/salary-inferred posting would miss reuse forever.
        # Stamped => pre-enrich-vs-pre-enrich match; unstamped => post-enrich fallback (fail-safe:
        # reuse merely misses next build and re-enriches, never serves stale enrichment).
        "enrich_hash": job._enrich_input_hash
        if job._enrich_input_hash is not None
        else enrich_hash(job),
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
        "degree_min": job.degree_min,
        "degree_required": (
            None if job.degree_required is None else (1 if job.degree_required else 0)
        ),
        "visa_sponsor": 1 if job.visa_sponsor else None,
        "visa_last_filed": job.visa_last_filed,
        "sponsorship_offered": (
            None if job.sponsorship_offered is None else (1 if job.sponsorship_offered else 0)
        ),
        "apply_url": job.apply_url,
        "listing_url": job.apply_url,
        "board_token": job.board_token,
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
    dr = row["degree_required"]
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
        degree_min=row["degree_min"],
        degree_required=(None if dr is None else bool(dr)),
        apply_url=row["apply_url"],
        posted_at=_parse_dt(row["posted_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        visa_sponsor=True if row["visa_sponsor"] == 1 else None,
        visa_last_filed=row["visa_last_filed"],
        sponsorship_offered=(None if sp is None else bool(sp)),
    )
