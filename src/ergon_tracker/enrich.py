"""Post-normalization enrichment: run the field extractors over a posting and write the
results onto the ``JobPosting`` (level, sector, salary-from-text, years-of-experience), then
normalize each location.

The per-field logic lives in the ``ergon_tracker.extract`` package; this module is the orchestrator
plus backward-compatible re-exports.
"""

from __future__ import annotations

from .extract.base import get_extractor, input_from_job

# Importing the extractor modules registers them. Also re-exported for backward compatibility.
from .extract.comp import CompExtractor  # noqa: F401
from .extract.degree import DegreeExtractor  # noqa: F401
from .extract.geo import has_us_signal, normalize_geo
from .extract.lang import detect_language
from .extract.level import (  # noqa: F401
    LevelExtractor,
    infer_level,
    level_from_description,
    level_from_years,
)
from .extract.sector import SectorExtractor, SectorIndex, load_sector_index  # noqa: F401
from .extract.sponsorship import detect_sponsorship  # noqa: F401
from .extract.visa import h1b_last_filed, is_h1b_sponsor, load_sponsor_index  # noqa: F401
from .extract.yoe import YoeExtractor  # noqa: F401
from .models import JobLevel, JobPosting

__all__ = ["enrich_in_place", "infer_level", "normalize_geo", "load_sector_index", "SectorIndex"]


def enrich_in_place(
    job: JobPosting,
    *,
    company_key: str | None = None,
    infer_level_from_experience: bool = True,
) -> JobPosting:
    """Enrich a posting in place: level, salary (from text if missing), years-of-experience,
    sector, and normalized locations. Existing values are never overwritten.

    ``infer_level_from_experience`` (on by default): when the title gives no level, derive a
    coarse level from the extracted years-of-experience. On, it trades a little precision for
    much higher coverage (58% of indexed jobs have no title-level signal); pass ``False`` to
    keep ``level`` strictly title-based.
    """
    inp = input_from_job(job, company_key=company_key)
    # Detect the JD's language (stdlib stopword heuristic; fails safe to "en" on anything short,
    # ambiguous, or genuinely English) so downstream extractors pick the right vocab table. This
    # is the only place language gets set — extractors never guess it themselves.
    inp.language = detect_language(inp.description_text)

    # years-of-experience first, so the optional level fallback below can use it.
    yoe = get_extractor("yoe")
    if yoe is not None and job.years_experience_min is None and job.years_experience_max is None:
        job.years_experience_min, job.years_experience_max = yoe.extract(inp)

    # Minimum-degree requirement (deterministic gazetteer over the description); tri-state
    # scope like sponsorship. Skipped when a provider already populated either field.
    degree = get_extractor("degree")
    if degree is not None and job.degree_min is None and job.degree_required is None:
        job.degree_min, job.degree_required = degree.extract(inp)

    level = get_extractor("level")
    if level is not None and job.level is JobLevel.UNKNOWN:
        job.level = level.extract(inp)
    if infer_level_from_experience and job.level is JobLevel.UNKNOWN:
        # 1) explicit early-career phrase in the JD ("new grad", "entry-level") — high precision;
        # 2) else map the stated years-of-experience requirement to a coarse level.
        job.level = level_from_description(inp.description_text)
        if job.level is JobLevel.UNKNOWN:
            job.level = level_from_years(job.years_experience_min, job.years_experience_max)

    comp = get_extractor("comp")
    if comp is not None and job.salary is None:
        job.salary = comp.extract(inp)

    sector = get_extractor("sector")
    if sector is not None and job.sector is None:
        job.sector = sector.extract(inp)

    # H-1B sponsor: positive evidence only (set True when matched; leave None otherwise).
    # Skip federal postings (USAJOBS): US government roles generally require citizenship, so an
    # employer-name match against LCA data there is a false positive (e.g. "Veterans Health
    # Administration") that would mislead visa-dependent applicants into dead-end applications.
    if job.visa_sponsor is None and job.source != "usajobs" and is_h1b_sponsor(job.company):
        job.visa_sponsor = True
        job.visa_last_filed = h1b_last_filed(job.company)

    # Posting-stated sponsorship policy (regex over the description); tri-state, often unknown.
    # Uses inp.description_text, which falls back to stripped description_html for aggregators.
    if job.sponsorship_offered is None:
        job.sponsorship_offered = detect_sponsorship(inp.description_text)

    for loc in job.locations:
        normalize_geo(loc)
        # Workday multi-location placeholders ("3 Locations") often hide an unambiguous US
        # posting. When normalization found no country, default WORKDAY postings to the US —
        # but ONLY when the raw string carries a US-specific signal (state name/abbrev or a
        # ZIP-like token); never a blanket default.
        if loc.country is None and job.source == "workday" and has_us_signal(loc.raw):
            loc.country = "United States"
    return job
