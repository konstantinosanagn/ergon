"""Canonical data models for ergon_tracker — the FROZEN CONTRACT all providers normalize to.

Every provider produces ``RawJob`` from its source, then maps it into a ``JobPosting``.
Missing fields are ``None`` (or an ``UNKNOWN`` enum) — never invented.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RemoteType",
    "EmploymentType",
    "SalaryInterval",
    "JobLevel",
    "DEGREE_LEVELS",
    "DEGREE_ORDER",
    "Location",
    "Salary",
    "Company",
    "RawJob",
    "Provenance",
    "JobPosting",
    "SearchQuery",
    "SourceHealth",
    "SearchResult",
    "make_job_id",
]


class RemoteType(str, Enum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    OTHER = "other"
    UNKNOWN = "unknown"


class SalaryInterval(str, Enum):
    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"


class JobLevel(str, Enum):
    """Seniority ladder, inferred from the job title during enrichment."""

    INTERN = "intern"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"
    UNKNOWN = "unknown"


# Minimum-degree ladder (order = strictness) shared by the extractor, the SearchQuery filter,
# and the index SQL. Values are the ``degree_min`` enum; DEGREE_ORDER gives each its rank so
# "keep jobs whose degree_min <= max_degree" is a simple integer compare.
DEGREE_LEVELS: tuple[str, ...] = ("highschool", "associate", "bachelor", "master", "phd_md")
DEGREE_ORDER: dict[str, int] = {name: i for i, name in enumerate(DEGREE_LEVELS)}
_DegreeLevel = Literal["highschool", "associate", "bachelor", "master", "phd_md"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_job_id(source: str, source_job_id: str) -> str:
    """Stable, short id derived from (source, source_job_id)."""
    return hashlib.sha1(f"{source}:{source_job_id}".encode()).hexdigest()[:16]


class Location(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None
    raw: str | None = None
    is_remote: bool = False

    def as_text(self) -> str:
        parts = [p for p in (self.city, self.region, self.country) if p]
        return ", ".join(parts) if parts else (self.raw or "")


class Salary(BaseModel):
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    interval: SalaryInterval | None = None

    def as_text(self) -> str:
        """Human-readable salary, robust to partial data.

        Handles min-only, max-only, equal min==max, missing currency/interval, and the
        empty case — so callers can format a salary without a "max=None blew up the format
        string" crash. Returns "" when there is no amount at all.
        """
        lo, hi = self.min_amount, self.max_amount
        if lo is None and hi is None:
            return ""
        prefix = f"{self.currency} " if self.currency else ""
        if lo is not None and hi is not None and lo != hi:
            body = f"{lo:,.0f}–{hi:,.0f}"
        else:
            body = f"{(lo if lo is not None else hi):,.0f}"
        suffix = f"/{self.interval.value}" if self.interval else ""
        return f"{prefix}{body}{suffix}"


class Company(BaseModel):
    """Canonical employer identity (keyed by dedup.normalize_company)."""

    company_key: str
    display_name: str
    domain: str | None = None
    primary_ats: str | None = None
    board_token: str | None = None
    sector: str | None = None
    h1b_sponsor: bool | None = None
    h1b_last_filed: str | None = None
    open_roles: int = 0
    first_seen: str | None = None
    last_seen: str | None = None


class RawJob(BaseModel):
    """Pre-normalization container: exactly what a source returned for one posting."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str
    source_job_id: str
    company: str
    token: str | None = None
    url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=_utcnow)


class Provenance(BaseModel):
    """A record of one source that yielded a (possibly merged) posting."""

    source: str
    source_job_id: str
    apply_url: str | None = None
    fetched_at: datetime = Field(default_factory=_utcnow)


class JobPosting(BaseModel):
    """The canonical, normalized posting returned to users."""

    id: str
    source: str
    source_job_id: str
    company: str
    company_domain: str | None = None
    title: str
    description_text: str | None = None
    description_html: str | None = None
    locations: list[Location] = Field(default_factory=list)
    remote: RemoteType = RemoteType.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    level: JobLevel = JobLevel.UNKNOWN
    department: str | None = None
    sector: str | None = None
    salary: Salary | None = None
    years_experience_min: int | None = None
    years_experience_max: int | None = None
    apply_url: str | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    # Relevance score for the current query (set by the ranking layer; None when unranked).
    # Higher is more relevant. Transient/query-dependent — not part of the stored posting.
    score: float | None = None
    # H-1B visa-sponsor signal: True if the employer appears in DoL LCA certified-filing data.
    # None = unknown (absence is NOT proof a company doesn't sponsor — the data is historical).
    visa_sponsor: bool | None = None
    # Most-recent certified H-1B filing date (ISO 'YYYY-MM-DD') for this employer, when known —
    # lets a user judge whether a sponsor has gone quiet since then.
    visa_last_filed: str | None = None
    # What the POSTING itself says about visa sponsorship (regex over the description):
    # True = offered ("sponsorship available"), False = explicitly not ("must not require
    # sponsorship"), None = not stated (common). Distinct from visa_sponsor (employer-level DoL).
    sponsorship_offered: bool | None = None
    # Minimum degree the posting asks for (see DEGREE_LEVELS), extracted from the description.
    # None = the posting doesn't state an education requirement (common). No competitor job API
    # exposes this field — it's what makes new-grad filtering actually work.
    degree_min: str | None = None
    # Scope of that degree ask (tri-state, mirrors sponsorship_offered): True = required,
    # False = preferred-only / "or equivalent experience", None = degree stated without scope.
    # NOTE: "strongly preferred" is still False — but max_degree filtering excludes on degree_min
    # regardless of scope, because a "strongly preferred M.D./Ph.D." effectively gates new grads.
    degree_required: bool | None = None

    @classmethod
    def create(
        cls,
        *,
        source: str,
        source_job_id: str | int,
        company: str,
        title: str,
        fetched_at: datetime | None = None,
        apply_url: str | None = None,
        **fields: Any,
    ) -> JobPosting:
        """Ergonomic constructor for providers.

        Auto-fills ``id`` and a default single-entry ``provenance`` so normalize() code stays
        short and consistent. Pass any other ``JobPosting`` field via kwargs.
        """
        sid = str(source_job_id)
        fetched = fetched_at or _utcnow()
        provenance = fields.pop("provenance", None) or [
            Provenance(source=source, source_job_id=sid, apply_url=apply_url, fetched_at=fetched)
        ]
        return cls(
            id=make_job_id(source, sid),
            source=source,
            source_job_id=sid,
            company=company,
            title=title,
            apply_url=apply_url,
            provenance=provenance,
            **fields,
        )


class SearchQuery(BaseModel):
    """A unified query, plus the client-side ``matches()`` filter.

    Keyword handling is hybrid by design:

    * **Server-side pre-filter:** ``adzuna``, ``smartrecruiters``, ``usajobs``, ``workday`` and
      ``workable_network`` pass ``keywords`` straight to their remote API, so they only return
      matching postings (and, for paginating Workday / the Workable network feed, fetch fewer
      pages). See each provider's ``fetch``.
    * **Client-side filter (every other provider):** Greenhouse/Lever/Ashby/etc. have **no**
      keyword API — their boards return everything, so ``matches()`` is the keyword filter applied
      after fetch. The cost there is the number of boards, not the number of jobs.

    All structured filters (level, geo, salary, years, sector, sponsorship, …) are always applied
    client-side by ``matches()`` regardless of source.
    """

    keywords: str | None = None
    location: str | None = None
    remote: bool | None = None
    employment_type: EmploymentType | None = None
    posted_after: datetime | None = None
    # Freshness filter: drop postings whose most-recent activity (max of posted_at/updated_at) is
    # older than this many days. ATS boards often leave filled reqs open for years, so a posting's
    # presence is NOT proof it's active; this hides the stale tail. None = no freshness filter (the
    # model default, so existing callers are unchanged); the production surface (MCP) defaults to 365.
    max_age_days: int | None = None
    # Whether to keep postings with no date at all (default: drop them — undated correlates with stale
    # legacy reqs). Mirrors include_unknown_level; only consulted when max_age_days is set.
    include_undated: bool = False
    # Staleness backstop keyed on last_seen (the last build that CONFIRMED the posting present on its
    # board; carried-forward rows keep their true last-crawl date). Drops active rows whose board
    # hasn't been successfully re-crawled in this many days — the abandoned/erroring-board tail whose
    # snapshot carry_forward would otherwise surface indefinitely (e.g. a job deleted on greenhouse
    # that still shows "active" because its board went uncrawled). Distinct from max_age_days, which
    # keys on the posting's OWN date (posted_at/updated_at). None = off (model default, existing
    # callers unchanged); the production surface (MCP) sets a default. Must exceed the max legitimate
    # crawl gap (COLD 7d + quarantine 7d) so a slow-but-alive board is never hidden.
    max_last_seen_age_days: int | None = None
    limit: int | None = None
    companies: list[str] | None = None
    sources: list[str] | None = None
    # advanced filters (applied after enrichment)
    level: JobLevel | None = None
    # When filtering by level, also keep postings with no inferable level (default: drop them,
    # i.e. a strict filter). Mirrors include_unknown_salary/years for the inferred level field.
    include_unknown_level: bool = False
    country: str | None = None
    city: str | None = None
    sector: str | None = None
    # When filtering by sector, also keep postings with no sector (default: drop them).
    include_unknown_sector: bool = False
    # When True, keep only employers known to sponsor H-1B visas (DoL LCA data). None = no filter.
    visa_sponsor: bool | None = None
    # Filter on what the POSTING says about sponsorship. True = keep postings that offer it;
    # False = keep postings that explicitly don't. None = no filter. By default unknown postings
    # are KEPT (they're the majority) — set include_unknown_sponsorship=False for a strict filter.
    sponsorship_offered: bool | None = None
    include_unknown_sponsorship: bool = True
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    include_unknown_salary: bool = True
    min_years: int | None = None
    max_years: int | None = None
    include_unknown_years: bool = True
    # Education ceiling: keep jobs whose extracted degree_min is <= this level in the ordering
    # highschool < associate < bachelor < master < phd_md. Exclusion applies REGARDLESS of
    # degree_required — a "strongly preferred M.D./Ph.D." (degree_required=False) still gates a
    # new grad in practice, so max_degree="bachelor" drops it; degree_required is surfaced on
    # each result so consumers see the nuance. None = no degree filter.
    max_degree: _DegreeLevel | None = None
    # When filtering by max_degree, also keep postings with no stated degree (default: keep —
    # unspecified is the majority and absence isn't evidence of a requirement). Mirrors
    # include_unknown_years/include_unknown_sponsorship.
    include_unknown_degree: bool = True
    # opt-in: derive level from years-of-experience when the title has no seniority marker
    # (boosts level coverage; trades some precision — off by default)
    infer_level_from_experience: bool = False
    # opt-in: semantic search. Skips the exact-token keyword gate and ranks by embedding
    # similarity instead (needs the `semantic` extra). off by default = lexical BM25 ranking.
    semantic: bool = False

    def _years_ok(self, job: JobPosting) -> bool:
        if self.min_years is None and self.max_years is None:
            return True
        jmin = job.years_experience_min
        jmax = job.years_experience_max
        if jmin is None and jmax is None:
            return self.include_unknown_years
        job_lo = jmin if jmin is not None else jmax
        job_hi = jmax if jmax is not None else jmin
        if job_lo is None or job_hi is None:  # unreachable given the guard
            return self.include_unknown_years
        want_lo = self.min_years if self.min_years is not None else float("-inf")
        want_hi = self.max_years if self.max_years is not None else float("inf")
        return not (job_hi < want_lo or job_lo > want_hi)

    def _degree_ok(self, job: JobPosting) -> bool:
        """Degree-ceiling filter: keep when degree_min <= max_degree in the DEGREE_LEVELS order.

        Unknown-degree postings are kept unless include_unknown_degree=False. degree_required is
        deliberately NOT consulted: a preferred-only advanced degree still exceeds the ceiling
        (the William Blair case — "M.D. or Ph.D. ... strongly preferred" must be excludable by
        max_degree="bachelor"); the tri-state scope is exposed on the posting instead.
        """
        if self.max_degree is None:
            return True
        if job.degree_min is None:
            return self.include_unknown_degree
        # Unrecognized stored value (future level?) -> treat as exceeding every ceiling.
        rank = DEGREE_ORDER.get(job.degree_min, len(DEGREE_LEVELS))
        return rank <= DEGREE_ORDER[self.max_degree]

    def _salary_ok(self, job: JobPosting) -> bool:
        if self.salary_min is None and self.salary_max is None:
            return True
        s = job.salary
        if s is None or (s.min_amount is None and s.max_amount is None):
            return self.include_unknown_salary
        if (
            self.salary_currency
            and s.currency
            and s.currency.upper() != self.salary_currency.upper()
        ):
            return False
        job_lo = s.min_amount if s.min_amount is not None else s.max_amount
        job_hi = s.max_amount if s.max_amount is not None else s.min_amount
        if job_lo is None or job_hi is None:  # unreachable given the guard above
            return self.include_unknown_salary
        want_lo = self.salary_min if self.salary_min is not None else float("-inf")
        want_hi = self.salary_max if self.salary_max is not None else float("inf")
        # keep when the job's range overlaps the requested range
        return not (job_hi < want_lo or job_lo > want_hi)

    def _geo_ok(self, job: JobPosting) -> bool:
        if self.country:
            # Alias-resolved (USA/US -> United States), shared with the index SQL.
            from .extract.geo import country_matches

            if not any(
                country_matches(self.country, loc.country, loc.raw) for loc in job.locations
            ):
                return False
        if self.city:
            # Metro-aware city match (NYC boroughs/"NYC", "SF", ...) shared with the index SQL.
            from .extract.geo import city_matches

            if not any(city_matches(self.city, loc.city, loc.raw) for loc in job.locations):
                return False
        return True

    def matches(self, job: JobPosting) -> bool:
        # Semantic mode ranks by meaning, so it must NOT pre-filter on exact tokens here
        # (that would drop relevant postings that don't contain the literal words).
        if self.keywords and not self.semantic:
            haystack = " ".join(
                filter(
                    None,
                    [job.title, job.department, job.company, job.description_text or ""],
                )
            ).lower()
            if not all(tok in haystack for tok in self.keywords.lower().split()):
                return False

        if self.location:
            loc_text = " ".join(loc.as_text() for loc in job.locations).lower()
            if self.location.lower() not in loc_text:
                return False

        if self.remote is True:
            # Precise (mirrors the index SQL): keep a job only with a positive remote signal —
            # remote/hybrid, a remote location flag, or "remote" in the location text. Generic
            # UNKNOWN-remote jobs with no such signal are dropped (a remote filter that returns
            # every untagged onsite posting is useless).
            loc_text = " ".join(loc.as_text() for loc in job.locations).lower()
            is_remote = (
                job.remote in (RemoteType.REMOTE, RemoteType.HYBRID)
                or any(loc.is_remote for loc in job.locations)
                or "remote" in loc_text
            )
            if not is_remote:
                return False

        if self.employment_type and job.employment_type not in (
            self.employment_type,
            EmploymentType.UNKNOWN,
        ):
            return False

        # Level filter. include_unknown_level keeps postings whose level couldn't be inferred
        # (UNKNOWN), so the filter narrows without dropping the many real titles with no marker.
        if (
            self.level is not None
            and job.level != self.level
            and not (self.include_unknown_level and job.level is JobLevel.UNKNOWN)
        ):
            return False

        # Sector filter. include_unknown_sector keeps postings with no detected sector.
        if (
            self.sector
            and self.sector.lower() not in (job.sector or "").lower()
            and not (self.include_unknown_sector and not job.sector)
        ):
            return False

        # Visa-sponsor filter: when True, keep only employers with positive H-1B evidence.
        if self.visa_sponsor is True and job.visa_sponsor is not True:
            return False

        # Posting sponsorship-policy filter. Keeps matches; unknown postings kept unless the
        # caller opts out (include_unknown_sponsorship=False) for a strict filter.
        if (
            self.sponsorship_offered is not None
            and job.sponsorship_offered != self.sponsorship_offered
            and not (job.sponsorship_offered is None and self.include_unknown_sponsorship)
        ):
            return False

        if not self._geo_ok(job):
            return False

        if not self._salary_ok(job):
            return False

        if not self._years_ok(job):
            return False

        if not self._degree_ok(job):
            return False

        if self.max_age_days is not None:
            # Freshness floor on the most-recent activity (max of posted_at/updated_at); undated
            # postings are dropped unless include_undated. Mirrors the index _where freshness clause.
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
            dates = [d for d in (job.posted_at, job.updated_at) if d is not None]
            fresh = max(dates) if dates else None
            if fresh is None:
                if not self.include_undated:
                    return False
            elif fresh < cutoff:
                return False

        return not (
            self.posted_after is not None
            and job.posted_at is not None
            and job.posted_at < self.posted_after
        )


class SourceHealth(BaseModel):
    """Per-source outcome of a search. Surfaced so callers never see a silent empty result."""

    source: str
    ok: bool
    count: int = 0
    error: str | None = None
    elapsed_ms: int = 0
    truncated: bool = False
    # For snapshot sources (the prebuilt index): which build served the query, so callers can
    # judge data freshness. None for live sources (always current).
    as_of: str | None = None


class SearchResult(BaseModel):
    jobs: list[JobPosting] = Field(default_factory=list)
    health: list[SourceHealth] = Field(default_factory=list)

    def __iter__(self) -> Iterator[JobPosting]:  # type: ignore[override]
        return iter(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [j.model_dump(mode="json") for j in self.jobs]

    def to_pandas(self) -> Any:  # optional dep, dynamic return type
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError(
                "DataFrame export needs: pip install 'ergon-tracker[pandas]'"
            ) from exc
        return pd.DataFrame(self.to_dicts())

    def to_polars(self) -> Any:  # optional dep, dynamic return type
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError(
                "DataFrame export needs: pip install 'ergon-tracker[polars]'"
            ) from exc
        return pl.DataFrame(self.to_dicts())

    @property
    def ok_sources(self) -> list[str]:
        return [h.source for h in self.health if h.ok]

    @property
    def failed_sources(self) -> list[SourceHealth]:
        return [h for h in self.health if not h.ok]
