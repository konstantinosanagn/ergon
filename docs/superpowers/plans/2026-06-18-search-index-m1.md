# Search Index — M1 (Pipeline Proof) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundational, fully-offline-testable core of the broad-discovery search index: a canonical `Company` entity, a versioned SQLite/FTS5 schema, a builder that turns `JobPosting`s into an index, a query layer that mirrors `matches()`, a download/verify cache, and `run_search` routing that uses the index for broad queries with a guaranteed live fallback.

**Architecture:** Build plane = `build_index(jobs, path)` writes a single SQLite/FTS5 file (companies + jobs + provenance + FTS + meta) deterministically with `integrity_check`. Query plane = `SqliteIndexBackend` behind an `IndexBackend` Protocol; `IndexCache` fetches/verifies the published snapshot; `run_search` routes broad queries to the index (∪ live keyed APIs) and falls back to live on any failure. All canonicalization reuses existing code (`normalize_company`, `normalize_title`, `make_job_id`, `deduplicate`).

**Tech Stack:** Python 3.10+, stdlib `sqlite3` (FTS5), pydantic v2, anyio/httpx (existing `AsyncFetcher`), pytest. No new runtime dependencies.

**Scope:** M1 only. Smart tiering / incremental crawl / throttle back-pressure = M2. Full observability + data-quality gate suite + GitHub Action = M3. Sector shards + deltas = Approach B. M1 includes a *minimal* build script (bounded crawl) so the index can be dogfooded live, plus a single integrity gate.

**Spec:** `docs/superpowers/specs/2026-06-18-search-index-design.md`

---

### Task 1: `Company` canonical model

**Files:**
- Modify: `src/ergon_tracker/models.py` (add `Company`; add to `__all__`)
- Test: `tests/test_company_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_company_model.py
from ergon_tracker.models import Company

def test_company_defaults_and_fields():
    c = Company(company_key="stripe", display_name="Stripe")
    assert c.company_key == "stripe"
    assert c.open_roles == 0
    assert c.domain is None and c.h1b_sponsor is None

def test_company_is_exported():
    import ergon_tracker.models as m
    assert "Company" in m.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_company_model.py -q`
Expected: FAIL (`ImportError: cannot import name 'Company'`)

- [ ] **Step 3: Add the model**

In `src/ergon_tracker/models.py`, add after the `Salary` class:

```python
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
```

Add `"Company",` to the `__all__` list.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_company_model.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/models.py tests/test_company_model.py
git commit -m "feat(index): add canonical Company model"
```

---

### Task 2: Company canonicalizer (`aggregate_companies`)

**Files:**
- Create: `src/ergon_tracker/canonicalize.py`
- Test: `tests/test_canonicalize.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_canonicalize.py
from ergon_tracker.canonicalize import aggregate_companies
from ergon_tracker.models import JobPosting

def _job(company, **kw):
    return JobPosting.create(source="greenhouse", source_job_id=company+kw.get("t",""),
                             company=company, title="Engineer", **{k:v for k,v in kw.items() if k!="t"})

def test_aggregate_keys_by_normalized_name_and_counts_open_roles():
    jobs = [_job("Stripe, Inc.", t="1"), _job("STRIPE INC", t="2"), _job("Acme GmbH", t="3")]
    by_key = {c.company_key: c for c in aggregate_companies(jobs)}
    assert by_key["stripe"].open_roles == 2     # both Stripe variants collapse
    assert by_key["acme"].open_roles == 1
    assert by_key["stripe"].display_name in ("Stripe, Inc.", "STRIPE INC")

def test_aggregate_fills_domain_and_sector_when_present():
    jobs = [_job("Stripe", company_domain=None, sector=None),
            _job("Stripe", company_domain="stripe.com", sector="Fintech")]
    c = aggregate_companies(jobs)[0]
    assert c.domain == "stripe.com" and c.sector == "Fintech"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_canonicalize.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.canonicalize`)

- [ ] **Step 3: Implement the canonicalizer**

```python
# src/ergon_tracker/canonicalize.py
"""Aggregate canonical JobPostings into canonical Company entities (reuses dedup keys)."""

from __future__ import annotations

from .dedup import normalize_company
from .extract.visa import h1b_last_filed, is_h1b_sponsor
from .models import Company, JobPosting

__all__ = ["aggregate_companies"]


def aggregate_companies(jobs: list[JobPosting]) -> list[Company]:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_canonicalize.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/canonicalize.py tests/test_canonicalize.py
git commit -m "feat(index): aggregate_companies canonicalizer (reuses normalize_company)"
```

---

### Task 3: Versioned schema + DB connection helper

**Files:**
- Create: `src/ergon_tracker/index/__init__.py` (empty)
- Create: `src/ergon_tracker/index/schema.sql`
- Create: `src/ergon_tracker/index/db.py`
- Test: `tests/test_index_db.py`
- Modify: `pyproject.toml` (ensure `index/schema.sql` ships in the wheel)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_db.py
from ergon_tracker.index.db import SCHEMA_VERSION, connect, fresh_db

def test_fresh_db_has_expected_tables(tmp_path):
    p = tmp_path / "i.sqlite"
    fresh_db(p)
    con = connect(p)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"companies", "jobs", "job_sources", "job_events", "job_tags", "meta"} <= names
    # FTS5 virtual table present
    fts = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE name='jobs_fts'")}
    assert "jobs_fts" in fts

def test_check_constraint_rejects_bad_remote(tmp_path):
    import sqlite3, pytest
    p = tmp_path / "i.sqlite"; fresh_db(p); con = connect(p)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO jobs(id,content_hash,source,company,title,remote,level,"
                    "employment_type,status,first_seen,last_seen,fetched_at,build_id) "
                    "VALUES('a','h','greenhouse','Co','T','BOGUS','mid','full_time',"
                    "'active','d','d','d','b')")

def test_schema_version_is_int():
    assert isinstance(SCHEMA_VERSION, int) and SCHEMA_VERSION >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_db.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.db`)

- [ ] **Step 3a: Write `schema.sql`**

```sql
-- src/ergon_tracker/index/schema.sql  (SCHEMA_VERSION must match db.py)
PRAGMA foreign_keys = ON;

CREATE TABLE companies (
  company_key TEXT PRIMARY KEY, display_name TEXT, domain TEXT,
  primary_ats TEXT, board_token TEXT, sector TEXT,
  h1b_sponsor INTEGER, h1b_last_filed TEXT, open_roles INTEGER,
  first_seen TEXT, last_seen TEXT
);

CREATE TABLE jobs (
  rowid INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  company_key TEXT REFERENCES companies(company_key),
  source TEXT NOT NULL, company TEXT NOT NULL, company_domain TEXT,
  title TEXT NOT NULL, department TEXT, role_family TEXT,
  location TEXT, city TEXT, country TEXT,
  remote TEXT NOT NULL CHECK (remote IN ('onsite','hybrid','remote','unknown')),
  level TEXT NOT NULL CHECK (level IN ('intern','entry','junior','mid','senior','staff',
                                       'principal','lead','manager','director','executive','unknown')),
  employment_type TEXT NOT NULL, sector TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT, salary_annual REAL,
  years_min INTEGER, years_max INTEGER,
  visa_sponsor INTEGER CHECK (visa_sponsor IN (1)),
  visa_last_filed TEXT,
  sponsorship_offered INTEGER CHECK (sponsorship_offered IN (0,1)),
  apply_url TEXT, listing_url TEXT, board_token TEXT,
  posted_at TEXT, updated_at TEXT, closes_at TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired')),
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  expired_at TEXT, expiry_reason TEXT,
  fetched_at TEXT NOT NULL, build_id TEXT NOT NULL, snippet TEXT,
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max)
);

CREATE TABLE job_sources (
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source TEXT NOT NULL, source_job_id TEXT NOT NULL, apply_url TEXT, fetched_at TEXT NOT NULL,
  PRIMARY KEY (job_id, source, source_job_id)
);

CREATE TABLE job_events (
  job_id TEXT, build_id TEXT, at TEXT,
  event TEXT CHECK (event IN ('appeared','changed','expired','reappeared'))
);

CREATE TABLE job_tags (job_id TEXT, tag TEXT, kind TEXT);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE VIRTUAL TABLE jobs_fts USING fts5(
  title, company, department, snippet,
  content='jobs', content_rowid='rowid',
  tokenize="porter unicode61 remove_diacritics 2"
);

CREATE INDEX idx_jobs_company_key ON jobs(company_key);
CREATE INDEX idx_jobs_level ON jobs(level);
CREATE INDEX idx_jobs_sector ON jobs(sector);
CREATE INDEX idx_jobs_country ON jobs(country);
CREATE INDEX idx_jobs_remote ON jobs(remote);
CREATE INDEX idx_jobs_role_family ON jobs(role_family);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_posted_at ON jobs(posted_at);
CREATE INDEX idx_jobs_visa ON jobs(visa_sponsor) WHERE visa_sponsor = 1;
CREATE INDEX idx_jobs_sponsorship ON jobs(sponsorship_offered) WHERE sponsorship_offered = 1;
CREATE INDEX idx_jobs_active ON jobs(status) WHERE status = 'active';
CREATE INDEX idx_jobsrc_job ON job_sources(job_id);
```

- [ ] **Step 3b: Write `db.py`**

```python
# src/ergon_tracker/index/db.py
"""SQLite connection + fresh-DB helpers for the search index."""

from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 1


def _schema_sql() -> str:
    return (files("ergon_tracker.index") / "schema.sql").read_text(encoding="utf-8")


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA query_only = ON")
    else:
        con = sqlite3.connect(str(path))
        con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


def fresh_db(path: Path | str) -> None:
    """Create a new index DB with the full schema (overwrites any existing file)."""
    p = Path(path)
    p.unlink(missing_ok=True)
    con = connect(p)
    try:
        con.executescript(_schema_sql())
        con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        con.commit()
    finally:
        con.close()
```

- [ ] **Step 3c: Ship `schema.sql` in the wheel**

In `pyproject.toml`, under `[tool.hatch.build.targets.wheel.force-include]`, add:

```toml
"src/ergon_tracker/index/schema.sql" = "ergon_tracker/index/schema.sql"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_index_db.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/index/ tests/test_index_db.py pyproject.toml
git commit -m "feat(index): versioned SQLite/FTS5 schema + db helpers"
```

---

### Task 4: Row mapping (`to_row` / `from_row`)

**Files:**
- Create: `src/ergon_tracker/index/mapping.py`
- Test: `tests/test_index_mapping.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_mapping.py
from datetime import datetime, timezone
from ergon_tracker.index.mapping import to_row, from_row
from ergon_tracker.models import JobPosting, Location, Salary, SalaryInterval, JobLevel, RemoteType

def _job():
    return JobPosting.create(
        source="greenhouse", source_job_id="1", company="Stripe", title="Senior Backend Engineer",
        company_domain="stripe.com", description_text="Build payments. Rust and Go.",
        locations=[Location(city="Berlin", country="Germany", raw="Berlin, Germany")],
        remote=RemoteType.REMOTE, level=JobLevel.SENIOR, sector="Fintech",
        salary=Salary(min_amount=120000, max_amount=160000, currency="USD", interval=SalaryInterval.YEAR),
        apply_url="https://x/1", posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        visa_sponsor=True, visa_last_filed="2026-03-31", sponsorship_offered=True,
    )

def test_round_trip_preserves_indexed_fields():
    j = _job()
    row = to_row(j, build_id="b1")
    j2 = from_row(row)
    assert j2.id == j.id and j2.company == "Stripe" and j2.title == j.title
    assert j2.level is JobLevel.SENIOR and j2.remote is RemoteType.REMOTE
    assert j2.sector == "Fintech" and j2.visa_sponsor is True and j2.sponsorship_offered is True
    assert j2.salary.min_amount == 120000 and j2.salary.currency == "USD"
    assert j2.locations[0].city == "Berlin" and j2.locations[0].country == "Germany"

def test_to_row_sets_role_family_and_company_key():
    from ergon_tracker.dedup import normalize_company, normalize_title
    row = to_row(_job(), build_id="b1")
    assert row["company_key"] == normalize_company("Stripe")
    assert row["role_family"] == normalize_title("Senior Backend Engineer")
    assert row["snippet"].startswith("Build payments")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_mapping.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.mapping`)

- [ ] **Step 3: Implement mapping**

```python
# src/ergon_tracker/index/mapping.py
"""The single JobPosting <-> SQLite row mapping (build + read share it)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..dedup import normalize_company, normalize_title
from ..models import JobLevel, JobPosting, Location, RemoteType, Salary, SalaryInterval

_SNIPPET = 300


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def to_row(job: JobPosting, *, build_id: str, now: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc).date().isoformat()
    loc = job.locations[0] if job.locations else None
    s = job.salary
    desc = job.description_text or ""
    return {
        "id": job.id,
        "content_hash": job.id,  # M1: id is stable; M2 introduces a real content_hash
        "company_key": normalize_company(job.company),
        "source": job.source, "company": job.company, "company_domain": job.company_domain,
        "title": job.title, "department": job.department,
        "role_family": normalize_title(job.title),
        "location": loc.as_text() if loc else None,
        "city": loc.city if loc else None, "country": loc.country if loc else None,
        "remote": job.remote.value, "level": job.level.value,
        "employment_type": job.employment_type.value, "sector": job.sector,
        "salary_min": s.min_amount if s else None, "salary_max": s.max_amount if s else None,
        "salary_currency": s.currency if s else None,
        "salary_interval": s.interval.value if s and s.interval else None,
        "salary_annual": None,
        "years_min": job.years_experience_min, "years_max": job.years_experience_max,
        "visa_sponsor": 1 if job.visa_sponsor else None,
        "visa_last_filed": job.visa_last_filed,
        "sponsorship_offered": (None if job.sponsorship_offered is None
                                else (1 if job.sponsorship_offered else 0)),
        "apply_url": job.apply_url, "listing_url": job.apply_url, "board_token": None,
        "posted_at": _iso(job.posted_at), "updated_at": _iso(job.updated_at), "closes_at": None,
        "status": "active", "first_seen": now, "last_seen": now,
        "expired_at": None, "expiry_reason": None,
        "fetched_at": now, "build_id": build_id, "snippet": desc[:_SNIPPET] or None,
    }


def from_row(row: Any) -> JobPosting:
    sal = None
    if row["salary_min"] is not None or row["salary_max"] is not None:
        sal = Salary(
            min_amount=row["salary_min"], max_amount=row["salary_max"],
            currency=row["salary_currency"],
            interval=SalaryInterval(row["salary_interval"]) if row["salary_interval"] else None,
        )
    locs = []
    if row["location"] or row["city"] or row["country"]:
        locs = [Location(city=row["city"], country=row["country"], raw=row["location"],
                         is_remote=row["remote"] == "remote")]
    sp = row["sponsorship_offered"]
    return JobPosting(
        id=row["id"], source=row["source"], source_job_id=row["id"],
        company=row["company"], company_domain=row["company_domain"], title=row["title"],
        description_text=row["snippet"], department=row["department"], sector=row["sector"],
        locations=locs, remote=RemoteType(row["remote"]), level=JobLevel(row["level"]),
        salary=sal, years_experience_min=row["years_min"], years_experience_max=row["years_max"],
        apply_url=row["apply_url"],
        visa_sponsor=True if row["visa_sponsor"] == 1 else None,
        visa_last_filed=row["visa_last_filed"],
        sponsorship_offered=(None if sp is None else bool(sp)),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_index_mapping.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/index/mapping.py tests/test_index_mapping.py
git commit -m "feat(index): JobPosting<->row mapping (single source of truth)"
```

---

### Task 5: Index builder

**Files:**
- Create: `src/ergon_tracker/index/build.py`
- Test: `tests/test_index_build.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_build.py
from ergon_tracker.index.build import build_index
from ergon_tracker.index.db import connect
from ergon_tracker.models import JobPosting, Location, RemoteType, JobLevel

def _job(sid, company, title, **kw):
    return JobPosting.create(source=kw.pop("source", "greenhouse"), source_job_id=sid,
                             company=company, title=title,
                             locations=[Location(raw="Remote", is_remote=True)],
                             remote=RemoteType.REMOTE, **kw)

def test_build_writes_rows_companies_fts_and_passes_integrity(tmp_path):
    p = tmp_path / "i.sqlite"
    jobs = [_job("1", "Stripe", "Senior Backend Engineer", level=JobLevel.SENIOR, sector="Fintech"),
            _job("2", "Stripe", "Frontend Engineer")]
    n = build_index(jobs, p, build_id="b1")
    assert n == 2
    con = connect(p, read_only=True)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert con.execute("SELECT open_roles FROM companies WHERE company_key='stripe'").fetchone()[0] == 2
    # FTS keyword search works
    hits = con.execute("SELECT j.title FROM jobs j JOIN jobs_fts f ON j.rowid=f.rowid "
                       "WHERE jobs_fts MATCH 'backend'").fetchall()
    assert any("Backend" in h[0] for h in hits)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

def test_build_dedups_same_job_from_two_sources(tmp_path):
    p = tmp_path / "i.sqlite"
    jobs = [_job("1", "Stripe", "Senior Backend Engineer", source="greenhouse"),
            _job("x", "Stripe", "Sr. Backend Engineer", source="remoteok")]
    build_index(jobs, p, build_id="b1")
    con = connect(p, read_only=True)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1  # merged
    assert con.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] >= 2  # provenance kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_build.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.build`)

- [ ] **Step 3: Implement the builder**

```python
# src/ergon_tracker/index/build.py
"""Build a SQLite/FTS5 index file from canonical JobPostings (deterministic, integrity-checked)."""

from __future__ import annotations

from pathlib import Path

from ..canonicalize import aggregate_companies
from ..dedup import deduplicate
from ..models import JobPosting
from .db import SCHEMA_VERSION, connect, fresh_db
from .mapping import to_row

_JOB_COLS = (
    "id content_hash company_key source company company_domain title department role_family "
    "location city country remote level employment_type sector salary_min salary_max "
    "salary_currency salary_interval salary_annual years_min years_max visa_sponsor "
    "visa_last_filed sponsorship_offered apply_url listing_url board_token posted_at updated_at "
    "closes_at status first_seen last_seen expired_at expiry_reason fetched_at build_id snippet"
).split()


class IndexBuildError(RuntimeError):
    pass


def build_index(jobs: list[JobPosting], path: Path | str, *, build_id: str) -> int:
    """Dedup -> write companies + jobs + provenance + FTS + meta. Returns row count."""
    deduped = deduplicate(jobs)
    deduped.sort(key=lambda j: j.id)  # deterministic order
    fresh_db(path)
    con = connect(path)
    try:
        companies = aggregate_companies(deduped)
        con.executemany(
            "INSERT INTO companies(company_key,display_name,domain,primary_ats,board_token,"
            "sector,h1b_sponsor,h1b_last_filed,open_roles,first_seen,last_seen) "
            "VALUES(:company_key,:display_name,:domain,:primary_ats,:board_token,:sector,"
            ":h1b_sponsor,:h1b_last_filed,:open_roles,:first_seen,:last_seen)",
            [{**c.model_dump(), "h1b_sponsor": 1 if c.h1b_sponsor else None} for c in companies],
        )
        placeholders = ",".join(":" + c for c in _JOB_COLS)
        con.executemany(
            f"INSERT INTO jobs({','.join(_JOB_COLS)}) VALUES({placeholders})",
            [to_row(j, build_id=build_id) for j in deduped],
        )
        con.executemany(
            "INSERT OR IGNORE INTO job_sources(job_id,source,source_job_id,apply_url,fetched_at) "
            "VALUES(?,?,?,?,?)",
            [(j.id, p.source, p.source_job_id, p.apply_url, p.fetched_at.isoformat())
             for j in deduped for p in j.provenance],
        )
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('build_id',?)", (build_id,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('row_count',?)",
                    (str(len(deduped)),))
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('optimize')")
        con.commit()
        con.execute("ANALYZE")
        con.execute("VACUUM")
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise IndexBuildError(f"integrity_check failed: {ok}")
        return len(deduped)
    finally:
        con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_index_build.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/index/build.py tests/test_index_build.py
git commit -m "feat(index): deterministic index builder (dedup+companies+FTS+integrity)"
```

---

### Task 6: Query layer (`SearchQuery` → SQL) + `matches()` parity

**Files:**
- Create: `src/ergon_tracker/index/query.py`
- Test: `tests/test_index_query.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_query.py
from ergon_tracker.index.build import build_index
from ergon_tracker.index.db import connect
from ergon_tracker.index.query import search_rows
from ergon_tracker.models import JobPosting, JobLevel, SearchQuery, Location, RemoteType

def _job(sid, title, **kw):
    return JobPosting.create(source="greenhouse", source_job_id=sid, company=kw.pop("company","Co"),
                             title=title, locations=[Location(raw="Remote", is_remote=True)],
                             remote=RemoteType.REMOTE, **kw)

def _db(tmp_path, jobs):
    p = tmp_path / "i.sqlite"; build_index(jobs, p, build_id="b1"); return connect(p, read_only=True)

def test_keyword_ranks_title_match_first(tmp_path):
    con = _db(tmp_path, [
        _job("1", "Account Executive", description_text="work with engineering and engineer teams"),
        _job("2", "Software Engineer", description_text="build services"),
    ])
    rows = search_rows(con, SearchQuery(keywords="engineer", limit=5))
    assert rows[0]["title"] == "Software Engineer"  # title hit beats description-only hit

def test_filter_only_path_and_level_filter(tmp_path):
    con = _db(tmp_path, [_job("1","Eng",level=JobLevel.SENIOR), _job("2","Eng",level=JobLevel.MID)])
    rows = search_rows(con, SearchQuery(level=JobLevel.SENIOR, limit=10))
    assert len(rows) == 1 and rows[0]["level"] == "senior"

def test_matches_parity_on_filters(tmp_path):
    jobs = [_job("1","Eng",level=JobLevel.SENIOR, sector="Fintech"),
            _job("2","Eng",level=JobLevel.MID, sector="Fintech"),
            _job("3","Eng",level=JobLevel.SENIOR, sector=None)]
    con = _db(tmp_path, jobs)
    for q in [SearchQuery(level=JobLevel.SENIOR),
              SearchQuery(sector="Fintech"),
              SearchQuery(sector="Fintech", include_unknown_sector=True),
              SearchQuery(level=JobLevel.SENIOR, include_unknown_level=True)]:
        sql_ids = {r["id"] for r in search_rows(con, q)}
        match_ids = {j.id for j in jobs if q.matches(j)}
        assert sql_ids == match_ids, f"parity broke for {q}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_query.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.query`)

- [ ] **Step 3: Implement the query translator**

```python
# src/ergon_tracker/index/query.py
"""Translate a SearchQuery into SQL over the index, mirroring SearchQuery.matches() semantics."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..models import SearchQuery

_TOKEN = re.compile(r"[a-z0-9]+")


def _match_expr(keywords: str) -> str:
    toks = _TOKEN.findall(keywords.lower())
    return " AND ".join(f'"{t}"' for t in toks)  # quoted = no FTS5 syntax injection


def _where(q: SearchQuery) -> tuple[list[str], list[Any]]:
    cl: list[str] = ["j.status = 'active'"]
    p: list[Any] = []
    if q.remote is True:
        cl.append("(j.remote IN ('remote','hybrid'))")
    if q.level is not None:
        if q.include_unknown_level:
            cl.append("(j.level = ? OR j.level = 'unknown')"); p.append(q.level.value)
        else:
            cl.append("j.level = ?"); p.append(q.level.value)
    if q.sector:
        if q.include_unknown_sector:
            cl.append("(LOWER(j.sector) LIKE ? OR j.sector IS NULL)"); p.append(f"%{q.sector.lower()}%")
        else:
            cl.append("LOWER(j.sector) LIKE ?"); p.append(f"%{q.sector.lower()}%")
    if q.country:
        cl.append("LOWER(j.country) = ?"); p.append(q.country.lower())
    if q.city:
        cl.append("LOWER(j.city) = ?"); p.append(q.city.lower())
    if q.visa_sponsor is True:
        cl.append("j.visa_sponsor = 1")
    if q.sponsorship_offered is not None:
        v = 1 if q.sponsorship_offered else 0
        if q.include_unknown_sponsorship:
            cl.append("(j.sponsorship_offered = ? OR j.sponsorship_offered IS NULL)"); p.append(v)
        else:
            cl.append("j.sponsorship_offered = ?"); p.append(v)
    if q.salary_min is not None:
        cl.append("(j.salary_max IS NULL OR j.salary_max >= ?)" if q.include_unknown_salary
                  else "j.salary_max >= ?"); p.append(q.salary_min)
    if q.salary_max is not None:
        cl.append("(j.salary_min IS NULL OR j.salary_min <= ?)" if q.include_unknown_salary
                  else "j.salary_min <= ?"); p.append(q.salary_max)
    return cl, p


def search_rows(con: sqlite3.Connection, q: SearchQuery) -> list[sqlite3.Row]:
    where, params = _where(q)
    limit = q.limit or 1000
    if q.keywords:
        sql = ("SELECT j.* FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid "
               "WHERE jobs_fts MATCH ? AND " + " AND ".join(where) +
               " ORDER BY bm25(jobs_fts, 10,3,3,1) LIMIT ?")
        return con.execute(sql, [_match_expr(q.keywords), *params, limit]).fetchall()
    sql = ("SELECT j.* FROM jobs j WHERE " + " AND ".join(where) +
           " ORDER BY j.posted_at DESC LIMIT ?")
    return con.execute(sql, [*params, limit]).fetchall()
```

> Note: M1 covers the filters with clean SQL equivalents (level, sector, country, city, remote, visa_sponsor, sponsorship_offered, salary). The years filter and `posted_after` are added in M2 alongside the same parity test extended to them. Keep the parity test's query list to filters implemented here.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_index_query.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/index/query.py tests/test_index_query.py
git commit -m "feat(index): SearchQuery->SQL translator with matches() parity"
```

---

### Task 7: `IndexBackend` interface + `SqliteIndexBackend`

**Files:**
- Create: `src/ergon_tracker/index/backend.py`
- Test: `tests/test_index_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_backend.py
from ergon_tracker.index.build import build_index
from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.models import JobPosting, SearchQuery, Location, RemoteType, JobLevel

def _job(sid, title, **kw):
    return JobPosting.create(source="greenhouse", source_job_id=sid, company=kw.pop("company","Co"),
                             title=title, locations=[Location(raw="Remote", is_remote=True)],
                             remote=RemoteType.REMOTE, **kw)

def test_backend_search_returns_jobpostings_with_provenance(tmp_path):
    p = tmp_path / "i.sqlite"
    build_index([_job("1","Senior Backend Engineer", level=JobLevel.SENIOR)], p, build_id="b1")
    be = SqliteIndexBackend(p)
    assert be.available() is True
    assert be.metadata()["row_count"] == 1
    jobs = be.search(SearchQuery(keywords="backend", limit=5))
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].provenance and jobs[0].provenance[0].source == "greenhouse"

def test_backend_unavailable_when_missing(tmp_path):
    be = SqliteIndexBackend(tmp_path / "nope.sqlite")
    assert be.available() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_backend.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.backend`)

- [ ] **Step 3: Implement the backend**

```python
# src/ergon_tracker/index/backend.py
"""IndexBackend protocol + the SQLite implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import JobPosting, Provenance, SearchQuery
from .db import SCHEMA_VERSION, connect
from .mapping import from_row
from .query import search_rows


@runtime_checkable
class IndexBackend(Protocol):
    def available(self) -> bool: ...
    def metadata(self) -> dict: ...
    def search(self, query: SearchQuery) -> list[JobPosting]: ...


class SqliteIndexBackend:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def available(self) -> bool:
        if not self.path.exists():
            return False
        try:
            con = connect(self.path, read_only=True)
            v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            con.close()
            return bool(v) and int(v[0]) == SCHEMA_VERSION
        except Exception:  # noqa: BLE001 - any open/read failure => not usable
            return False

    def metadata(self) -> dict:
        con = connect(self.path, read_only=True)
        try:
            meta = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM meta")}
            return {"schema_version": int(meta.get("schema_version", 0)),
                    "build_id": meta.get("build_id"),
                    "row_count": int(meta.get("row_count", 0))}
        finally:
            con.close()

    def search(self, query: SearchQuery) -> list[JobPosting]:
        con = connect(self.path, read_only=True)
        try:
            rows = search_rows(con, query)
            jobs = []
            for row in rows:
                job = from_row(row)
                src = con.execute(
                    "SELECT source,source_job_id,apply_url,fetched_at FROM job_sources WHERE job_id=?",
                    (job.id,)).fetchall()
                job.provenance = [Provenance(source=s["source"], source_job_id=s["source_job_id"],
                                             apply_url=s["apply_url"]) for s in src] or job.provenance
                jobs.append(job)
            return jobs
        finally:
            con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_index_backend.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/index/backend.py tests/test_index_backend.py
git commit -m "feat(index): IndexBackend protocol + SqliteIndexBackend"
```

---

### Task 8: `IndexCache` (download / verify / freshness)

**Files:**
- Create: `src/ergon_tracker/index/cache.py`
- Test: `tests/test_index_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_cache.py
import gzip, hashlib, json
from ergon_tracker.index.build import build_index
from ergon_tracker.index.cache import IndexCache
from ergon_tracker.models import JobPosting

def _publish(remote_dir, tmp_path):
    src = tmp_path / "src.sqlite"
    build_index([JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Eng")],
                src, build_id="b1")
    raw = src.read_bytes()
    gz = remote_dir / "index.sqlite.gz"; gz.write_bytes(gzip.compress(raw))
    (remote_dir / "manifest.json").write_text(json.dumps(
        {"build_id": "b1", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
         "schema_version": 1}))

def test_cache_downloads_verifies_and_opens(tmp_path):
    remote = tmp_path / "remote"; remote.mkdir(); _publish(remote, tmp_path)
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    path = cache.ensure_fresh()
    assert path is not None and path.exists()
    from ergon_tracker.index.backend import SqliteIndexBackend
    assert SqliteIndexBackend(path).available() is True

def test_cache_rejects_corrupt_download(tmp_path):
    remote = tmp_path / "remote"; remote.mkdir(); _publish(remote, tmp_path)
    (remote / "manifest.json").write_text(json.dumps(
        {"build_id":"b1","sha256":"0"*64,"bytes":1,"schema_version":1}))  # wrong sha
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    assert cache.ensure_fresh() is None  # verify fails -> no usable index
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_cache.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.cache`)

- [ ] **Step 3: Implement the cache**

```python
# src/ergon_tracker/index/cache.py
"""Download + verify + freshness-gate the published index snapshot."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import urllib.request
from pathlib import Path

from .db import SCHEMA_VERSION

log = logging.getLogger("ergon_tracker.index")
_DEFAULT_BASE = "https://github.com/konstantinosanagn/ergon-tracker/releases/latest/download"


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "ergon-tracker"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - https/file only
        return r.read()


class IndexCache:
    def __init__(self, base_url: str | None = None, cache_dir: Path | None = None) -> None:
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.cache_dir = Path(cache_dir or _default_cache_dir())
        self.db_path = self.cache_dir / "index.sqlite"
        self.local_manifest = self.cache_dir / "manifest.json"

    def ensure_fresh(self) -> Path | None:
        """Return a path to a verified, schema-compatible index, or None (→ caller live-falls-back)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            remote = json.loads(_fetch(f"{self.base_url}/manifest.json"))
        except Exception as exc:  # noqa: BLE001
            log.warning("index manifest fetch failed (%s); using cache if present", exc)
            return self.db_path if self.db_path.exists() else None
        if int(remote.get("schema_version", -1)) != SCHEMA_VERSION:
            log.warning("index schema_version mismatch; live fallback")
            return None
        local = json.loads(self.local_manifest.read_text()) if self.local_manifest.exists() else {}
        if local.get("build_id") == remote.get("build_id") and self.db_path.exists():
            return self.db_path  # already current
        try:
            raw = gzip.decompress(_fetch(f"{self.base_url}/index.sqlite.gz"))
        except Exception as exc:  # noqa: BLE001
            log.warning("index download failed (%s)", exc)
            return self.db_path if self.db_path.exists() else None
        if hashlib.sha256(raw).hexdigest() != remote.get("sha256"):
            log.warning("index sha256 mismatch; rejecting download")
            return None
        tmp = self.db_path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(self.db_path)  # atomic
        self.local_manifest.write_text(json.dumps(remote))
        log.info("index updated to build %s (%d bytes)", remote.get("build_id"), len(raw))
        return self.db_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_index_cache.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/ergon_tracker/index/cache.py tests/test_index_cache.py
git commit -m "feat(index): IndexCache download/verify/freshness with live-fallback returns"
```

---

### Task 9: Route broad queries through the index (with live fallback)

**Files:**
- Modify: `src/ergon_tracker/engine.py` (add index routing at the top of `run_search`)
- Create: `src/ergon_tracker/index/router.py` (decide + load backend; keeps engine thin)
- Test: `tests/test_index_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_routing.py
import ergon_tracker.index.router as router
from ergon_tracker.index.build import build_index
from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.models import JobPosting, SearchQuery, JobLevel

def test_router_uses_index_for_broad_query(tmp_path, monkeypatch):
    p = tmp_path / "i.sqlite"
    build_index([JobPosting.create(source="greenhouse", source_job_id="1", company="Co",
                                   title="Senior Backend Engineer", level=JobLevel.SENIOR)],
                p, build_id="b1")
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))
    out = router.try_index(SearchQuery(keywords="backend", limit=5))
    assert out is not None and out[0].title == "Senior Backend Engineer"

def test_router_returns_none_for_targeted_query(monkeypatch):
    # companies-targeted => index is bypassed (caller goes live)
    assert router.try_index(SearchQuery(keywords="x", companies=["stripe.com"])) is None

def test_router_returns_none_when_index_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(tmp_path / "none.sqlite"))
    assert router.try_index(SearchQuery(keywords="x")) is None

def test_env_off_disables_index(monkeypatch):
    monkeypatch.setenv("ERGON_INDEX", "off")
    assert router.try_index(SearchQuery(keywords="x")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_index_routing.py -q`
Expected: FAIL (`ModuleNotFoundError: ergon_tracker.index.router`)

- [ ] **Step 3: Implement the router**

```python
# src/ergon_tracker/index/router.py
"""Decide whether a query should be served from the index, and do it safely (never raise)."""

from __future__ import annotations

import logging
import os

from ..models import JobPosting, SearchQuery
from .backend import SqliteIndexBackend
from .cache import IndexCache

log = logging.getLogger("ergon_tracker.index")


def _load_backend() -> SqliteIndexBackend | None:
    path = IndexCache().ensure_fresh()
    return SqliteIndexBackend(path) if path else None


def try_index(query: SearchQuery) -> list[JobPosting] | None:
    """Return index results for a broad query, or None to signal 'fall back to live'."""
    if os.environ.get("ERGON_INDEX", "").lower() == "off":
        return None
    if query.companies or query.sources:  # targeted => live (fresher, already fast)
        return None
    try:
        backend = _load_backend()
        if backend is None or not backend.available():
            return None
        return backend.search(query)
    except Exception as exc:  # noqa: BLE001 - index is a fast path, never a hard dependency
        log.warning("index query failed (%s); live fallback", exc)
        return None
```

- [ ] **Step 4: Wire it into `run_search`**

In `src/ergon_tracker/engine.py`, at the very start of `run_search` (after `load_builtins()/load_plugins()`), add:

```python
    from .index.router import try_index

    indexed = try_index(query)
    if indexed is not None:
        # Broad query served from the index; merge live keyed search APIs for freshness/coverage.
        # (M1: return the index result directly; live keyed-API merge lands in M2.)
        return SearchResult(jobs=indexed, health=[build_health("index", ok=True, count=len(indexed))])
```

(`build_health` is already imported in `engine.py`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_index_routing.py -q`
Expected: PASS (4 passed)

- [ ] **Step 6: Run the full suite (no regressions)**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all previous tests + new). Live/targeted tests unaffected (router returns None for them).

- [ ] **Step 7: Commit**

```bash
git add src/ergon_tracker/index/router.py src/ergon_tracker/engine.py tests/test_index_routing.py
git commit -m "feat(index): route broad queries to the index with guaranteed live fallback"
```

---

### Task 10: Minimal build script (dogfood a real index)

**Files:**
- Create: `scripts/build_index.py`
- Test: `tests/test_build_index_script.py`

- [ ] **Step 1: Write the failing test (pure helper, no network)**

```python
# tests/test_build_index_script.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_index import publish_artifacts  # noqa: E402
from ergon_tracker.index.build import build_index
from ergon_tracker.models import JobPosting

def test_publish_writes_gz_and_manifest(tmp_path):
    src = tmp_path / "i.sqlite"
    build_index([JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Eng")],
                src, build_id="b1")
    out = tmp_path / "dist"
    publish_artifacts(src, out, build_id="b1")
    import json, hashlib, gzip
    man = json.loads((out / "manifest.json").read_text())
    assert man["build_id"] == "b1" and man["schema_version"] == 1
    raw = gzip.decompress((out / "index.sqlite.gz").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == man["sha256"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_build_index_script.py -q`
Expected: FAIL (`ModuleNotFoundError: build_index`)

- [ ] **Step 3: Implement the script**

```python
# scripts/build_index.py
"""M1 build entry: crawl a bounded slice of the registry -> build index -> publish artifacts.

Usage:
  .venv/bin/python scripts/build_index.py --limit-companies 300 --out dist/
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.build import build_index  # noqa: E402


def publish_artifacts(db_path: Path, out_dir: Path, *, build_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = db_path.read_bytes()
    (out_dir / "index.sqlite.gz").write_bytes(gzip.compress(raw))
    (out_dir / "manifest.json").write_text(json.dumps({
        "build_id": build_id, "schema_version": 1,
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
    }))


async def _crawl(limit_companies: int) -> list:
    import anyio  # noqa: F401  (anyio.run is the entry below)
    from ergon_tracker.engine import _plan_targets, run_search  # reuse the live engine
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.registry.store import SeedRegistry

    keys = list(SeedRegistry().all())[:limit_companies]
    q = SearchQuery(companies=keys)  # bounded crawl via existing engine
    async with AsyncFetcher() as fetcher:
        result = await run_search(q, fetcher)
    return result.jobs


def main(argv: list[str]) -> None:
    import anyio
    limit = 300
    out = ROOT / "dist"
    i = 0
    while i < len(argv):
        if argv[i] == "--limit-companies":
            limit = int(argv[i + 1]); i += 2
        elif argv[i] == "--out":
            out = Path(argv[i + 1]); i += 2
        else:
            print(f"unknown flag: {argv[i]}"); return
    build_id = "m1-local"  # M2 replaces with a timestamp passed from CI
    jobs = anyio.run(_crawl, limit)
    db = out / "index.sqlite"
    out.mkdir(parents=True, exist_ok=True)
    n = build_index(jobs, db, build_id=build_id)
    publish_artifacts(db, out, build_id=build_id)
    print(f"built index: {n} jobs -> {out}/index.sqlite.gz (+manifest.json)")


if __name__ == "__main__":
    main(sys.argv[1:])
```

> Note: `_crawl` routes through the live engine targeting registry companies — but `try_index` returns None for company-targeted queries, so the build crawl always hits live boards (no recursion into the index). For M1 this is the bounded full-crawl that produces the snapshot; M2 replaces it with the tiered incremental crawler.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_build_index_script.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/build_index.py tests/test_build_index_script.py
git commit -m "feat(index): minimal bounded build script + publish artifacts"
```

---

### Task 11: End-to-end offline integration test

**Files:**
- Test: `tests/test_index_e2e.py`

- [ ] **Step 1: Write the E2E test**

```python
# tests/test_index_e2e.py
"""Build -> publish to a temp 'release' -> cache downloads+verifies -> query -> live fallback."""

import ergon_tracker.index.router as router
from ergon_tracker.index.build import build_index
from ergon_tracker.index.cache import IndexCache
from ergon_tracker.models import JobPosting, SearchQuery, JobLevel, Location, RemoteType

def _jobs():
    return [
        JobPosting.create(source="greenhouse", source_job_id="1", company="Stripe",
                          title="Senior Backend Engineer", level=JobLevel.SENIOR, sector="Fintech",
                          locations=[Location(raw="Remote", is_remote=True)], remote=RemoteType.REMOTE),
        JobPosting.create(source="lever", source_job_id="2", company="Ramp",
                          title="Frontend Engineer",
                          locations=[Location(raw="Remote", is_remote=True)], remote=RemoteType.REMOTE),
    ]

def test_full_pipeline_offline(tmp_path, monkeypatch):
    import gzip, hashlib, json
    # build + publish to a temp "remote"
    src = tmp_path / "src.sqlite"; build_index(_jobs(), src, build_id="b1")
    remote = tmp_path / "remote"; remote.mkdir()
    raw = src.read_bytes()
    (remote / "index.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest.json").write_text(json.dumps(
        {"build_id":"b1","schema_version":1,"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}))
    # point the router's backend loader at a cache fed by the temp remote
    cache = IndexCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache")
    from ergon_tracker.index.backend import SqliteIndexBackend
    def _load():
        p = cache.ensure_fresh()
        return SqliteIndexBackend(p) if p else None
    monkeypatch.setattr(router, "_load_backend", _load)

    res = router.try_index(SearchQuery(keywords="backend", limit=10))
    assert res is not None and len(res) == 1 and res[0].company == "Stripe"

    # remove the index -> graceful fallback signal (None) without raising
    (cache.cache_dir / "index.sqlite").unlink()
    (remote / "index.sqlite.gz").unlink()
    (remote / "manifest.json").unlink()
    assert router.try_index(SearchQuery(keywords="backend", limit=10)) is None
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_index_e2e.py -q`
Expected: PASS (1 passed)

- [ ] **Step 3: Full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check src tests scripts`
Expected: all pass, ruff clean

- [ ] **Step 4: Commit**

```bash
git add tests/test_index_e2e.py
git commit -m "test(index): end-to-end offline pipeline (build->publish->cache->query->fallback)"
```

---

### Task 12: Live dogfood pass (mandatory — per working-style memory)

**Files:** none (verification + a short report appended to the plan or `INDEX_RUNS.md`)

- [ ] **Step 1: Build a small real index from live boards**

Run: `.venv/bin/python scripts/build_index.py --limit-companies 200 --out dist/`
Expected: prints `built index: N jobs -> dist/index.sqlite.gz (+manifest.json)` with N in the hundreds–thousands.

- [ ] **Step 2: Inspect the real DB**

Run:
```bash
.venv/bin/python - <<'PY'
from ergon_tracker.index.db import connect
con = connect("dist/index.sqlite", read_only=True)
print("rows:", con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
print("companies:", con.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
print("fts 'engineer':", con.execute("SELECT COUNT(*) FROM jobs j JOIN jobs_fts f ON j.rowid=f.rowid WHERE jobs_fts MATCH 'engineer'").fetchone()[0])
print("integrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
PY
```
Expected: non-zero rows/companies, FTS returns hits, integrity `ok`.

- [ ] **Step 3: Dogfood through the dev tools (SDK + CLI), pointing the cache at `dist/`**

```bash
# SDK + router against the freshly built local index:
ERGON_INDEX=on .venv/bin/python - <<'PY'
import ergon_tracker.index.router as router
from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.models import SearchQuery
router._load_backend = lambda: SqliteIndexBackend("dist/index.sqlite")
res = router.try_index(SearchQuery(keywords="engineer", limit=5))
for j in res: print(j.score if j.score is not None else "-", j.company, "|", j.title)
PY
```
Expected: ranked engineering roles, deduped, sane companies/titles. Confirm `ERGON_INDEX=off` makes `try_index` return None.

- [ ] **Step 4: Record findings**

Append a short note (rows, companies, query latency, anything surprising) to `INDEX_RUNS.md`. Commit:

```bash
git add INDEX_RUNS.md
git commit -m "docs(index): M1 live dogfood findings"
```

---

## Self-Review

**Spec coverage (M1 scope):** `Company` model + canonicalizer (T1–2) ✓; schema + lifecycle/navigation columns + FTS + indexes + constraints (T3) ✓; mapping single-source-of-truth (T4) ✓; deterministic builder with dedup/companies/FTS/integrity (T5) ✓; SearchQuery→SQL with `matches()` parity (T6) ✓; `IndexBackend` + SQLite impl (T7) ✓; cache download/verify/schema-gate/atomic/fallback (T8) ✓; routing broad→index + live fallback + `ERGON_INDEX=off` (T9) ✓; build/publish script + artifacts (T10) ✓; E2E offline (T11) ✓; live dogfood through dev tools (T12) ✓. Deferred to M2/M3 (documented): tiering/incremental/throttle-back-pressure, full gate suite + GH Action + observability, live keyed-API merge, years/`posted_after` filters, real `content_hash`, expiry/`job_events` population, sector shards (B).

**Placeholder scan:** none — every code/test step has complete content and exact commands.

**Type consistency:** `to_row`/`from_row` field names match `schema.sql` columns and `_JOB_COLS`; `search_rows(con, query)` signature consistent across query/backend/tests; `IndexCache.ensure_fresh() -> Path|None` consistent with `router._load_backend`; `build_index(jobs, path, *, build_id) -> int` consistent across builder/script/tests; `SCHEMA_VERSION=1` consistent in `db.py`, `schema.sql` comment, cache, manifests.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-18-search-index-m1.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
