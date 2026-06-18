"""Job-aware cross-source deduplication & merge engine — the ergon_tracker differentiator.

Naive dedup (exact id or exact title match) misses the common real-world case: the *same*
role surfaced by an ATS (greenhouse/lever/...) and by an aggregator (remoteok) with a slightly
different title ("Sr. Backend Engineer" vs "Senior Backend Engineer"). This module collapses
those into one canonical posting while keeping the richest, most-authoritative record and
unioning provenance so callers can still see every source that yielded the job.

Pure / in-memory / no network. Efficiency comes from *blocking*: fuzzy comparison only ever
happens between records that share a normalized company, never O(n^2) over the whole list.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .models import JobPosting, Provenance, RemoteType

__all__ = ["deduplicate", "normalize_title", "normalize_company", "blocking_key"]


# --------------------------------------------------------------------------------------------
# Authority model: who do we trust as the canonical record when two sources collide?
# ATS feeds come straight from the employer -> most authoritative. Aggregators re-list and
# often drop fields -> least authoritative. Anything else sits in between.
# --------------------------------------------------------------------------------------------
_ATS_PROVIDERS = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "workable",
        "recruitee",
        "personio",
        "bamboohr",
        "breezy",
        "teamtailor",
        "join",
        "rippling",
        "pinpoint",
        "eightfold",
        "successfactors",
        "oracle",
        "taleo",
        "icims",
        "avature",
        "jazzhr",
        "jobvite",
        "phenom",
        "brassring",
        "schemaorg",
        "apicapture",
    }
)
_AGGREGATORS = frozenset(
    {"remoteok", "remotive", "arbeitnow", "jobicy", "himalayas", "themuse", "adzuna", "usajobs"}
)

# Company legal-form suffixes / generic descriptors collapsed so "Acme Inc" == "Acme".
_COMPANY_STOPWORDS = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "ag",
        "sa",
        "sas",
        "bv",
        "srl",
        "oy",
        "ab",
        "pty",
        "holdings",
        "the",
    }
)

# Seniority / level filler stripped from titles so blocking + fuzzy compare role, not grade.
_TITLE_STOPWORDS = frozenset(
    {
        "senior",
        "sr",
        "snr",
        "junior",
        "jr",
        "jnr",
        "lead",
        "principal",
        "staff",
        "mid",
        "entry",
        "level",
        "i",
        "ii",
        "iii",
        "iv",
        "the",
        "a",
        "an",
    }
)

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("&", " and ")
    return [t for t in _PUNCT_RE.sub(" ", text).split() if t]


def normalize_company(company: str) -> str:
    """Lowercase, strip punctuation, and drop legal-form/alias suffixes.

    ``"Acme, Inc."`` and ``"Acme GmbH"`` both normalize to ``"acme"``.
    """
    toks = [t for t in _tokens(company) if t not in _COMPANY_STOPWORDS]
    # If everything was a stopword (unlikely), fall back to the raw normalized form.
    if not toks:
        toks = _tokens(company)
    return " ".join(toks)


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/whitespace, and remove seniority filler.

    ``"Sr. Backend Engineer"`` and ``"Senior  Backend Engineer"`` both normalize to
    ``"backend engineer"``.
    """
    toks = [t for t in _tokens(title) if t not in _TITLE_STOPWORDS]
    if not toks:
        toks = _tokens(title)
    return " ".join(toks)


def blocking_key(job: JobPosting) -> str:
    """Coarse key used to bucket candidate duplicates (normalized company + title).

    Comparison still happens across all blocks that share a company, so this key never causes
    a missed merge — it only narrows the fuzzy comparison space.
    """
    return f"{normalize_company(job.company)}|{normalize_title(job.title)}"


# --------------------------------------------------------------------------------------------
# Location compatibility
# --------------------------------------------------------------------------------------------
def _job_cities(job: JobPosting) -> set[str]:
    return {loc.city.strip().lower() for loc in job.locations if loc.city}


def _is_remote(job: JobPosting) -> bool:
    return job.remote in (RemoteType.REMOTE, RemoteType.HYBRID) or any(
        loc.is_remote for loc in job.locations
    )


def _compatible_location(a: JobPosting, b: JobPosting) -> bool:
    """Two postings are location-compatible if they share a city, or either side is
    remote / has no known city (unknown locations never block a merge)."""
    if _is_remote(a) or _is_remote(b):
        return True
    ca, cb = _job_cities(a), _job_cities(b)
    if not ca or not cb:
        return True
    return bool(ca & cb)


# --------------------------------------------------------------------------------------------
# Authority / completeness ranking
# --------------------------------------------------------------------------------------------
def _authority_rank(source: str) -> int:
    src = source.lower()
    if src in _ATS_PROVIDERS:
        return 0
    if src in _AGGREGATORS:
        return 2
    return 1


# Fields that count toward "completeness" and that we fill from secondaries when missing.
_FILLABLE_FIELDS = (
    "company_domain",
    "description_text",
    "description_html",
    "department",
    "salary",
    "apply_url",
    "posted_at",
    "updated_at",
)


def _completeness(job: JobPosting) -> int:
    score = sum(1 for f in _FILLABLE_FIELDS if getattr(job, f) is not None)
    if job.locations:
        score += 1
    if job.remote is not RemoteType.UNKNOWN:
        score += 1
    if job.employment_type.value != "unknown":
        score += 1
    return score


# --------------------------------------------------------------------------------------------
# Clustering + merge
# --------------------------------------------------------------------------------------------
class _Cluster:
    __slots__ = ("company", "members", "exact_keys")

    def __init__(self, company: str) -> None:
        self.company = company
        self.members: list[tuple[int, JobPosting]] = []
        self.exact_keys: set[tuple[str, str]] = set()

    @property
    def rep(self) -> JobPosting:
        return self.members[0][1]

    def add(self, index: int, job: JobPosting) -> None:
        self.members.append((index, job))
        self.exact_keys.add((job.source, job.source_job_id))


def _merge_cluster(members: list[tuple[int, JobPosting]]) -> JobPosting:
    """Collapse one cluster of duplicate postings into a single canonical JobPosting."""
    # Primary = most authoritative, then most complete, then earliest seen (stable).
    primary_index, primary = min(
        members,
        key=lambda im: (_authority_rank(im[1].source), -_completeness(im[1]), im[0]),
    )
    merged = primary.model_copy(deep=True)

    # Secondaries in the same priority order so the best donor fills first.
    secondaries = [
        job
        for idx, job in sorted(
            members,
            key=lambda im: (_authority_rank(im[1].source), -_completeness(im[1]), im[0]),
        )
        if not (idx == primary_index and job is primary)
    ]

    for sec in secondaries:
        for field in _FILLABLE_FIELDS:
            if getattr(merged, field) is None:
                value = getattr(sec, field)
                if value is not None:
                    setattr(merged, field, value)
        if not merged.locations and sec.locations:
            merged.locations = [loc.model_copy(deep=True) for loc in sec.locations]
        if merged.remote is RemoteType.UNKNOWN and sec.remote is not RemoteType.UNKNOWN:
            merged.remote = sec.remote
        if merged.employment_type.value == "unknown" and sec.employment_type.value != "unknown":
            merged.employment_type = sec.employment_type

    # Union provenance across every merged record, deduped by (source, source_job_id),
    # primary first.
    seen: set[tuple[str, str]] = set()
    union: list[Provenance] = []
    ordered = [primary] + secondaries
    for job in ordered:
        prov_entries = job.provenance or [
            Provenance(source=job.source, source_job_id=job.source_job_id, apply_url=job.apply_url)
        ]
        for prov in prov_entries:
            key = (prov.source, prov.source_job_id)
            if key not in seen:
                seen.add(key)
                union.append(prov.model_copy(deep=True))
    merged.provenance = union
    return merged


def deduplicate(jobs: list[JobPosting], *, threshold: float = 90.0) -> list[JobPosting]:
    """Return a NEW list of merged postings, order-stable by first occurrence.

    Pipeline:
      1. EXACT — identical ``(source, source_job_id)`` records collapse immediately.
      2. BLOCKING — candidates are bucketed by normalized company; fuzzy comparison only
         happens inside a company block (never global O(n^2)).
      3. FUZZY — within a block, ``rapidfuzz.token_sort_ratio`` on the normalized title must
         clear ``threshold`` AND the location must be compatible to count as a duplicate.
      4. MERGE — keep the richest/most-authoritative record (ATS > aggregator, then field
         completeness), union provenance, and backfill the primary's missing fields.
    """
    # Company-normalized block -> clusters in that block. Dict iteration is insertion-ordered,
    # but we emit by each cluster's first-occurrence index, so output order is fully stable.
    blocks: dict[str, list[_Cluster]] = {}
    exact_index: dict[tuple[str, str], _Cluster] = {}
    clusters_in_order: list[_Cluster] = []

    for index, job in enumerate(jobs):
        exact_key = (job.source, job.source_job_id)

        # 1. EXACT pass.
        existing = exact_index.get(exact_key)
        if existing is not None:
            existing.add(index, job)
            continue

        company = normalize_company(job.company)
        title = normalize_title(job.title)
        candidates = blocks.setdefault(company, [])

        # 2 + 3. Fuzzy match within the company block.
        matched: _Cluster | None = None
        for cluster in candidates:
            rep = cluster.rep
            score = fuzz.token_sort_ratio(title, normalize_title(rep.title))
            if score >= threshold and _compatible_location(job, rep):
                matched = cluster
                break

        if matched is None:
            matched = _Cluster(company)
            candidates.append(matched)
            clusters_in_order.append(matched)

        matched.add(index, job)
        exact_index[exact_key] = matched

    # Emit one merged posting per cluster, in first-occurrence order.
    ordered_clusters = sorted(clusters_in_order, key=lambda c: c.members[0][0])
    return [_merge_cluster(c.members) for c in ordered_clusters]
