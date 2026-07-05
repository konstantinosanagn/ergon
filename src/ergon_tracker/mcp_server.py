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

_INSTRUCTIONS = """\
ergon-tracker: unified job search across company ATS feeds (49k+ boards) + aggregators.

Index vs. live — how to choose your `search_jobs` call:
- "roles at <company>" → pass `companies=[...]` (domains/careers URLs). This fetches LIVE from
  that company's ATS: fresh and exact. Use it whenever the user names a specific employer.
- "find me <X> jobs" (broad discovery, no specific company) → leave `companies` and `sources`
  empty. This serves from the prebuilt INDEX: fast, throttle-proof, complete coverage across
  every board we track.
- Every index-served response carries `as_of` (the `build-…` id) in its `health`, so freshness
  is always visible. The index is a daily snapshot and can lag a known company's live board by a
  day or two — if a user needs guaranteed up-to-the-second results for a named company, target it
  LIVE via `companies=[...]` even for an otherwise-broad query.
- Passing ATS provider names in `sources` (e.g. greenhouse, lever) forces a deliberate live crawl
  of those providers — slower; only do it when explicitly scoping to a provider.
"""

mcp = FastMCP("ergon-tracker", instructions=_INSTRUCTIONS)


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
    max_degree: str | None = None,
    include_unknown_degree: bool = True,
    employment_type: str | None = None,
    posted_within_days: int | None = None,
    max_age_days: int | None = 365,
    include_undated: bool = False,
    visa_sponsor: bool = False,
    sponsorship_offered: bool | None = None,
    infer_level_from_experience: bool = True,
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
        max_degree: education ceiling — highschool / associate / bachelor / master / phd_md.
            Keeps postings whose extracted minimum degree is at or below this level (no other
            job API has an education field). Excludes even "preferred-only" advanced degrees —
            a "strongly preferred M.D./Ph.D." still gates new grads — while each job reports
            `degree_min` + `degree_required` (true=required, false=preferred-only, null=unstated)
            so you can show the nuance. Combine with min_years/max_years for new-grad searches:
            max_degree="bachelor", max_years=2.
        include_unknown_degree: keep postings with no stated degree requirement (default true).
        employment_type: full_time / part_time / contract / internship / temporary / other.
            Postings that don't state a type are kept.
        posted_within_days: only postings published within the last N days (recency filter).
        max_age_days: freshness floor (default 365). ATS boards often leave FILLED reqs open for
            years, so a posting's presence isn't proof it's active — this hides postings whose most
            recent activity (posted/updated) is older than N days. Pass null to include stale ones.
            The response echoes the floor actually used as `max_age_days_applied`.
        include_undated: keep postings with no date at all (default false; they correlate with stale).
        visa_sponsor: COMPANY-level signal. Each job's `visa_sponsor` flag means the EMPLOYER has
            certified H-1B LCA filings in US DoL data — a historical filer. Positive evidence
            only: true = the company has filed; null = no filing found, which is NOT "does not
            sponsor". It says nothing about whether THIS posting is sponsorable. Pass true to
            keep only known-filer employers.
        sponsorship_offered: POSTING-level signal — what THIS posting's own text states about
            visa sponsorship. Tri-state: true = the text offers it; false (0) = the posting
            EXPLICITLY refuses ("we cannot sponsor"); null = the text doesn't say (the
            majority). Filtering true keeps only offers; filtering false keeps only explicit
            refusals; unknowns are kept when unset. Each job reports `sponsorship_offered`.
        infer_level_from_experience: when a title has no seniority word, derive level from the
            required years of experience (default true — boosts level coverage; pass false for
            strictly title-based levels).
        semantic: rank by meaning via embeddings instead of exact-token matching (handles
            synonyms / natural-language intent). Needs the server's `semantic` extra.
        limit: max postings to return after dedup + ranking (default 20).

    Returns a dict with `count`, `jobs` (compact, relevance-ranked, each with a `score`),
    `max_age_days_applied` (the freshness floor used for this response), and per-source
    `health`.

    Examples (combine filters freely):
        # roles at specific companies
        search_jobs(keywords="engineer", companies=["stripe.com", "ramp.com"], level="senior")
        # broad, fast (auto aggregator APIs), well-paid + remote
        search_jobs(keywords="data scientist", remote=True, country="United States", salary_min=150000)
        # new-grad safe: drop roles gated on an advanced degree or 3+ years of experience
        search_jobs(keywords="research associate", max_degree="bachelor", max_years=2)
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
        max_degree=max_degree,  # type: ignore[arg-type]  # pydantic validates the literal
        include_unknown_degree=include_unknown_degree,
        employment_type=EmploymentType(employment_type) if employment_type else None,
        posted_after=_days_ago(posted_within_days),
        max_age_days=max_age_days,
        include_undated=include_undated,
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
        from .index.router import try_index_ranked

        indexed = try_index_ranked(query)  # index serving + semantic rerank (shared with engine)
        if indexed is not None:
            from .index.cache import cached_index_build_id

            return {
                "count": len(indexed),
                "max_age_days_applied": max_age_days,
                "jobs": [_job_to_dict(j) for j in indexed],
                "health": [
                    {
                        "source": "index",
                        "ok": True,
                        "count": len(indexed),
                        "error": None,
                        "as_of": cached_index_build_id(),  # which daily build served this (freshness)
                    }
                ],
            }
        query = query.model_copy(update={"sources": list(AGGREGATOR_PROVIDERS)})  # index down

    async with AsyncErgonTracker() as js:
        result = await js.search(query)
    return {
        "count": len(result.jobs),
        "max_age_days_applied": max_age_days,
        "jobs": [_job_to_dict(j) for j in result.jobs],
        "health": [h.model_dump() for h in result.health],
    }


@mcp.tool()
def whats_new(
    since_days: int = 7,
    keywords: str | None = None,
    location: str | None = None,
    remote: bool | None = None,
    level: str | None = None,
    include_unknown_level: bool = False,
    sector: str | None = None,
    include_unknown_sector: bool = False,
    country: str | None = None,
    city: str | None = None,
    salary_min: float | None = None,
    salary_max: float | None = None,
    visa_sponsor: bool = False,
    sponsorship_offered: bool | None = None,
    employment_type: str | None = None,
    include_changed: bool = False,
    limit: int = 25,
) -> dict[str, Any]:
    """What's NEW: jobs that first appeared in the daily index within the last `since_days`, newest first.

    A real change feed — the prebuilt index stamps `first_seen`/`updated_at` per job, so this answers
    "what showed up since I last looked" with ZERO ATS calls (throttle-proof, served locally). Every
    `search_jobs` filter composes with the recency cutoff, so you can ask e.g. "new senior fintech roles
    that sponsor H-1B this week." Index-only (live fetch has no first-seen history).

    Args:
        since_days: look back this many days by `first_seen` (default 7).
        include_changed: also include jobs whose details were UPDATED in the window (not just new ones).
        (all other args mirror `search_jobs`.) Each job carries `first_seen` and `is_new`.
    """
    from datetime import date, timedelta

    since_iso = (date.today() - timedelta(days=max(1, since_days))).isoformat()
    query = SearchQuery(
        keywords=keywords,
        location=location,
        remote=remote,
        level=JobLevel(level) if level else None,
        include_unknown_level=include_unknown_level,
        sector=sector,
        include_unknown_sector=include_unknown_sector,
        country=country,
        city=city,
        salary_min=salary_min,
        salary_max=salary_max,
        employment_type=EmploymentType(employment_type) if employment_type else None,
        visa_sponsor=True if visa_sponsor else None,
        sponsorship_offered=sponsorship_offered,
        limit=limit,
    )

    from .index.backend import SqliteIndexBackend
    from .index.cache import IndexCache, cached_index_build_id
    from .index.db import connect
    from .index.mapping import from_row
    from .index.query import whats_new_rows

    try:
        path = IndexCache().ensure_fresh()
    except Exception:  # noqa: BLE001 - index unavailable (offline / not yet built)
        path = None
    backend = SqliteIndexBackend(path) if path else None
    if backend is None or not backend.available():
        return {
            "count": 0,
            "since": since_iso,
            "jobs": [],
            "note": "prebuilt index unavailable — 'what's new' is served from the daily index",
        }

    assert path is not None  # narrowed: backend.available() implies a real index path
    con = connect(path, read_only=True)
    try:
        rows = whats_new_rows(con, query, since_iso, include_changed=include_changed)
    finally:
        con.close()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        d = _job_to_dict(from_row(row))
        d["first_seen"] = row["first_seen"]
        d["is_new"] = bool(row["first_seen"] and row["first_seen"] >= since_iso)
        if include_changed:
            d["updated_at"] = row["updated_at"]
        jobs.append(d)
    return {"count": len(jobs), "since": since_iso, "as_of": cached_index_build_id(), "jobs": jobs}


@mcp.tool()
def match_resume(
    resume: str,
    keywords: str | None = None,
    location: str | None = None,
    remote: bool | None = None,
    level: str | None = None,
    include_unknown_level: bool = True,
    sector: str | None = None,
    country: str | None = None,
    city: str | None = None,
    salary_min: float | None = None,
    visa_sponsor: bool = False,
    sponsorship_offered: bool | None = None,
    employment_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Rank jobs by SEMANTIC FIT to a résumé (or a target job description).

    Paste résumé text (or a JD, to find similar roles); the server embeds it and ranks postings by
    cosine similarity — matching on meaning, not shared keywords (so "built ML pipelines in PyTorch"
    surfaces "Machine Learning Engineer"). Two-stage: the index retrieves a filtered candidate pool,
    then embeddings rerank it by the full résumé. Served from the index → zero ATS calls.

    Needs the server's `semantic` extra; without it, degrades to lexical ranking (and says so).

    Args:
        resume: the résumé / profile / JD text to match against (required).
        keywords: optional top skills to focus retrieval (e.g. "python, kubernetes, fintech"); the
            résumé still drives the final fit ranking. All other args mirror `search_jobs` filters.
            Each returned job carries a `fit_score` (cosine similarity, ~0–1).
    """
    if not resume or not resume.strip():
        return {"count": 0, "jobs": [], "note": "provide résumé or JD text in `resume`"}

    query = SearchQuery(
        keywords=keywords,
        location=location,
        remote=remote,
        level=JobLevel(level) if level else None,
        include_unknown_level=include_unknown_level,
        sector=sector,
        country=country,
        city=city,
        salary_min=salary_min,
        employment_type=EmploymentType(employment_type) if employment_type else None,
        visa_sponsor=True if visa_sponsor else None,
        sponsorship_offered=sponsorship_offered,
        max_age_days=365,  # don't match a résumé against years-stale postings
        limit=limit,
    )
    from .index.router import try_index

    # Wide, filtered candidate pool from the index; then rerank the WHOLE pool by the full résumé.
    pool = try_index(query.model_copy(update={"limit": max(limit * 8, 120)}))
    if pool is None:
        return {
            "count": 0,
            "jobs": [],
            "note": "prebuilt index unavailable — résumé match is served from the daily index",
        }
    if not pool:
        return {"count": 0, "jobs": [], "note": "no candidates matched the filters; loosen them"}

    ranked_by = "semantic_fit"
    try:
        from .semantic import get_semantic_reranker

        scores = get_semantic_reranker().rerank(resume, pool)
        for j, s in zip(pool, scores, strict=True):
            j.score = round(float(s), 4)
    except Exception:  # noqa: BLE001 - semantic extra absent / model error -> lexical fallback
        from .ranking import rank

        rank(pool, keywords or resume, reranker=None)  # sets lexical job.score in place
        ranked_by = "lexical (install the server's `semantic` extra for embedding fit)"

    ranked = sorted(pool, key=lambda j: j.score if j.score is not None else 0.0, reverse=True)[
        :limit
    ]
    jobs = [{**_job_to_dict(j), "fit_score": j.score} for j in ranked]
    return {"count": len(jobs), "ranked_by": ranked_by, "jobs": jobs}


@mcp.tool()
def assess_fit(resume: str, job_description: str, job_title: str | None = None) -> dict[str, Any]:
    """Apply-assist: a deterministic résumé↔JD gap analysis to tailor an application.

    Paste your résumé and a target posting's description; get a structured breakdown — which listed
    skills your résumé already covers, which it's MISSING (the gaps to address), required vs your years
    of experience, and ready-to-use talking points. No LLM or API key needed (a curated skill gazetteer
    + rules); the calling agent uses this structure to draft a tailored cover letter / application
    answers grounded in the actual overlap, not guesses.

    Args:
        resume: your résumé / profile text.
        job_description: the target posting's description (ideally including requirements).
        job_title: optional posting title (sharpens the skill signal).
    """
    if not (resume or "").strip() or not (job_description or "").strip():
        return {"note": "provide both `resume` and `job_description` text"}

    from .extract.base import ExtractInput
    from .extract.skills import extract_skills
    from .extract.yoe import YoeExtractor

    jd_skills = extract_skills(f"{job_title or ''} {job_description}")
    cv_skills = extract_skills(resume)
    matched, missing = sorted(jd_skills & cv_skills), sorted(jd_skills - cv_skills)
    extra = sorted(cv_skills - jd_skills)
    coverage = round(len(matched) / len(jd_skills), 2) if jd_skills else None

    yoe = YoeExtractor()
    req_years = yoe.extract(ExtractInput(title=job_title or "", description_text=job_description))[
        0
    ]
    cv_min, cv_max = yoe.extract(ExtractInput(title="", description_text=resume))
    your_years = cv_max or cv_min
    meets_years = your_years is not None and req_years is not None and your_years >= req_years

    talking_points = [
        f"Lead with your {s} experience — it's a stated requirement." for s in matched[:6]
    ]
    gaps = [
        f"Address '{s}': not evident in your résumé — cite transferable work or willingness to ramp."
        for s in missing[:6]
    ]
    if req_years and (your_years is None or your_years < req_years):
        gaps.append(f"Role asks for ~{req_years}+ years; frame your experience to close that gap.")

    summary = (
        f"You match {len(matched)} of {len(jd_skills)} listed skills"
        + (f" ({int(coverage * 100)}%)" if coverage is not None else "")
        + (f"; gaps: {', '.join(missing[:5])}." if missing else " — strong coverage.")
    )
    return {
        "summary": summary,
        "matched_skills": matched,
        "missing_skills": missing,
        "extra_strengths": extra[:10],
        "skill_coverage": coverage,
        "required_years": req_years,
        "your_years": your_years,
        "meets_years": meets_years,
        "talking_points": talking_points,
        "gaps_to_address": gaps,
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
def h1b_jobs(
    keywords: str | None = None,
    location: str | None = None,
    remote: bool | None = None,
    level: str | None = None,
    include_unknown_level: bool = True,
    sector: str | None = None,
    country: str | None = None,
    city: str | None = None,
    salary_min: float | None = None,
    employment_type: str | None = None,
    min_filings: int = 0,
    active_within_years: int | None = None,
    sponsorship_offered: bool | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """H-1B-first job search: open roles at sponsor employers, each annotated with the employer's LCA
    filing VOLUME + most-recent filing, ranked by sponsor strength.

    The DoL-data-joined-to-live-jobs view a visa-dependent applicant actually needs — a strict superset
    of `search_jobs(visa_sponsor=True)`: it doesn't just filter to sponsors, it tells you HOW MUCH each
    employer sponsors (`h1b_filings`) and how RECENTLY (`h1b_last_filed` / `h1b_active`), and surfaces
    the strongest, most-active sponsors first. Served from the index → zero ATS calls.

    Args:
        min_filings: keep only employers with ≥ this many certified LCA filings (drop one-off / token
            sponsors). Default 0 (all sponsors).
        active_within_years: keep only employers that filed within the last N years (drop sponsors that
            have gone quiet). Default None (any).
        sponsorship_offered: if True, also require the POSTING itself to state sponsorship.
        (other args mirror `search_jobs`.) Each job carries `h1b_filings`, `h1b_last_filed`, `h1b_active`.
    """
    from datetime import date, timedelta

    query = SearchQuery(
        keywords=keywords,
        location=location,
        remote=remote,
        level=JobLevel(level) if level else None,
        include_unknown_level=include_unknown_level,
        sector=sector,
        country=country,
        city=city,
        salary_min=salary_min,
        employment_type=EmploymentType(employment_type) if employment_type else None,
        visa_sponsor=True,
        sponsorship_offered=sponsorship_offered,
        max_age_days=365,  # H-1B seekers need *current* openings, not filled-but-open reqs
        limit=limit,
    )
    from .index.router import try_index

    pool = try_index(query.model_copy(update={"limit": max(limit * 6, 100)}))
    if pool is None:
        return {
            "count": 0,
            "jobs": [],
            "note": "prebuilt index unavailable — H-1B job match is served from the daily index",
        }

    from .extract.visa import load_sponsor_index

    idx = load_sponsor_index()
    today = date.today()
    active_cut = (today - timedelta(days=730)).isoformat()  # "active" = filed within ~2 years
    user_cut = (
        (today - timedelta(days=365 * active_within_years)).isoformat()
        if active_within_years
        else None
    )

    rows: list[tuple[int, float, dict[str, Any]]] = []
    for j in pool:
        prof = idx.profile(j.company)
        fv = prof["filings"] if prof else 0
        filings = fv if isinstance(fv, int) else 0
        last = (
            str(prof["last_filed"]) if prof and prof["last_filed"] else None
        ) or j.visa_last_filed
        if filings == 0 and not j.visa_sponsor:  # defensive: only sponsor employers
            continue
        if filings < min_filings:
            continue
        if user_cut and (not last or last < user_cut):
            continue
        d = _job_to_dict(j)
        d["h1b_filings"] = filings
        d["h1b_last_filed"] = last
        d["h1b_active"] = bool(last and last >= active_cut)
        rows.append((filings, j.score or 0.0, d))

    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)  # strongest sponsor, then relevance
    return {
        "count": len(rows[:limit]),
        "ranked_by": "h1b_sponsor_strength",
        "jobs": [d for _, _, d in rows[:limit]],
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


@mcp.tool()
def list_companies(
    status: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Directory of the companies in our registry with their live posting status.

    Every company we track is either:
    - ``active``  — has >=1 live posting right now (``open_roles`` gives the count), or
    - ``dormant`` — registered (a confirmed real board) but with no current openings.

    Status + counts are derived from the latest daily index (recomputed each build, never a stale
    flag), so a dormant company flips to active automatically once it posts. Returns full totals
    (``registered``/``active``/``dormant``, plus ``index_only`` = companies with postings that
    aren't in our registry, e.g. aggregator-only) and a per-company list (each with ``company``,
    ``ats``, ``domain``, ``status``, ``open_roles``), sorted by ``open_roles`` desc.

    Args:
        status: filter the list to 'active' or 'dormant' (None = all). Counts are always full.
        query: case-insensitive substring on the company key.
        limit: max companies in the list (default 50; counts are unaffected).
    """
    import sqlite3

    from .index.cache import IndexCache, cached_index_build_id
    from .index.coverage import company_directory

    registry = dict(SeedRegistry().all())
    cache = IndexCache()
    db = None
    try:
        db = cache.ensure_fresh()  # download/refresh like the search path; cached copy on failure
    except Exception:  # noqa: BLE001 - index is a fast path, never a hard dependency
        db = None
    if db is None and cache.db_path.exists():
        db = cache.db_path
    if db is None or not db.exists():
        # No index available: we can still list the registry, all as dormant (unknown postings).
        return {
            "registered": len(registry),
            "active": 0,
            "dormant": len(registry),
            "index_only": 0,
            "as_of": None,
            "note": "index unavailable; posting status unknown (all shown dormant)",
            "companies": company_directory(
                _empty_companies_con(), registry, status=status, query=query, limit=limit
            )["companies"],
        }
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        result = company_directory(con, registry, status=status, query=query, limit=limit)
    finally:
        con.close()
    result["as_of"] = cached_index_build_id()
    return result


def _empty_companies_con() -> Any:
    """An in-memory connection with an empty ``companies`` table, so company_directory works (all
    dormant) when no index is available."""
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE companies(company_key TEXT, open_roles INTEGER)")
    return con


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
