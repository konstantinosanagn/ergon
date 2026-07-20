-- ergon-tracker search index schema. SCHEMA_VERSION must match db.py.
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
  -- Nullable (unlike content_hash): a prior-schema prev index carried forward via carry_forward()
  -- (build.py, _shared_cols) predates this column and has nothing to copy for it -- those rows
  -- stay NULL until next re-crawl. Every freshly-crawled row gets it unconditionally (mapping.to_row).
  enrich_hash TEXT,
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
  degree_min TEXT CHECK (degree_min IN ('highschool','associate','bachelor','master','phd_md')),
  degree_required INTEGER CHECK (degree_required IN (0,1)),
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
CREATE INDEX idx_jobs_degree ON jobs(degree_min);
CREATE INDEX idx_jobs_visa ON jobs(visa_sponsor) WHERE visa_sponsor = 1;
CREATE INDEX idx_jobs_sponsorship ON jobs(sponsorship_offered) WHERE sponsorship_offered = 1;
CREATE INDEX idx_jobs_active ON jobs(status) WHERE status = 'active';
CREATE INDEX idx_jobsrc_job ON job_sources(job_id);
