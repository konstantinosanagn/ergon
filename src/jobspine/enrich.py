"""Post-normalization enrichment: run the field extractors over a posting and write the
results onto the ``JobPosting`` (level, sector, salary-from-text, years-of-experience), then
normalize each location.

The per-field logic lives in the ``jobspine.extract`` package; this module is the orchestrator
plus backward-compatible re-exports.
"""

from __future__ import annotations

from .extract.base import get_extractor, input_from_job

# Importing the extractor modules registers them. Also re-exported for backward compatibility.
from .extract.comp import CompExtractor  # noqa: F401
from .extract.geo import normalize_geo
from .extract.level import LevelExtractor, infer_level  # noqa: F401
from .extract.sector import SectorExtractor, SectorIndex, load_sector_index  # noqa: F401
from .extract.yoe import YoeExtractor  # noqa: F401
from .models import JobLevel, JobPosting, Salary

__all__ = ["enrich_in_place", "infer_level", "normalize_geo", "load_sector_index", "SectorIndex"]


def enrich_in_place(job: JobPosting, *, company_key: str | None = None) -> JobPosting:
    """Enrich a posting in place: level, salary (from text if missing), years-of-experience,
    sector, and normalized locations. Existing values are never overwritten."""
    inp = input_from_job(job, company_key=company_key)

    level = get_extractor("level")
    if level is not None and job.level is JobLevel.UNKNOWN:
        job.level = level.extract(inp)

    comp = get_extractor("comp")
    if comp is not None and job.salary is None:
        parsed: Salary | None = comp.extract(inp)
        if parsed is not None:
            job.salary = parsed

    yoe = get_extractor("yoe")
    if yoe is not None and job.years_experience_min is None and job.years_experience_max is None:
        job.years_experience_min, job.years_experience_max = yoe.extract(inp)

    sector = get_extractor("sector")
    if sector is not None and job.sector is None:
        job.sector = sector.extract(inp)

    for loc in job.locations:
        normalize_geo(loc)
    return job
