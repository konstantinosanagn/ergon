# ergon-tracker

Unified, free job search over **30+ sources** (company ATS feeds + aggregators) in one Python
package. It fetches live postings, canonicalizes them into one schema, **dedupes** the same role
posted on many sites, enriches each posting (level, location, salary, years of experience, degree,
sector, **H-1B visa sponsorship**), and ranks by relevance — as an **SDK**, a **CLI**, and an
**MCP server** so humans *and* AI agents can use it.

> Names: install/repo = **`ergon-tracker`**, Python import = **`ergon_tracker`**, commands =
> **`ergon-tracker`** and **`ergon-tracker-mcp`**.

## What you get

- **One schema, deduped.** The same role from Greenhouse + RemoteOK + Adzuna collapses to **one**
  record (fuzzy title/company + location match; the employer ATS wins; every listing source is kept
  under `provenance`).
- **Two query paths, automatic.** *Targeted* (`companies=[...]`) fetches **live** from that
  company's ATS — freshest, exact. *Broad* (whole-registry) is served from a **free daily prebuilt
  index** — local, anonymous, **zero ATS contact at query time**, so no one gets rate-limited.
- **Typed filters.** Level, country/city, salary range + currency, years, degree, sector, remote,
  employment type, posting recency — all filterable and tested.
- **Visa-aware.** Per job: is the *employer* a known H-1B sponsor (US DoL LCA data + most-recent
  filing date), and does the *posting text* offer or refuse sponsorship. Both filterable.
- **Natural-language search.** BM25 by default (zero deps); optional local **semantic** embeddings
  (`[semantic]`, CPU, no API/GPU).
- **Agent-ready.** An MCP server exposes 9 tools (search, résumé match, fit assessment, H-1B, a
  change feed, …) with relevance `score`s and structured fields.

Everything is **free** — no paid APIs. Two optional sources (Adzuna, USAJOBS) use free keys you provide.

## Install

Not on PyPI yet — install from the repo:

```bash
git clone https://github.com/konstantinosanagn/ergon-tracker
cd ergon-tracker
uv venv && uv pip install -e ".[mcp]"     # or: python -m venv .venv && pip install -e ".[mcp]"
```

Extras: `[mcp]` (agent server), `[semantic]` (NL embedding search), `[pandas]`/`[polars]` (DataFrame export).

## Use it

### SDK

```python
from ergon_tracker import search

# Roles at a specific company (auto-detects its ATS):
res = search("engineer", companies=["stripe.com"], limit=10)
for job in res.jobs:
    loc = job.locations[0].as_text() if job.locations else "—"
    print(f"{job.score:5.1f}  {job.title}  [{loc}]  ({job.source})")

# Combinable typed filters:
res = search("backend", country="Germany", level="senior", salary_min=80000, remote=True, limit=20)

# New-grad, precise: SWE, ≤2 stated years, NYC metro, USD ≥ $140k, posted in the last 30 days:
from datetime import datetime, timedelta, timezone
res = search(
    "software engineer",
    city="New York",                             # matches NYC boroughs / "New York City" / "NYC"
    max_years=2, include_unknown_years=False,    # only roles that state ≤ 2 years
    salary_min=140_000, salary_currency="USD",   # USD only
    employment_type="full_time",
    posted_after=datetime.now(timezone.utc) - timedelta(days=30),
    limit=20,
)

# Semantic ranking (needs [semantic]):
res = search("AI and deep learning roles at fintechs", semantic=True, limit=10)
```

`search()` returns a `SearchResult`: `.jobs` (ranked, each with a `.score`), `.health` (per-source
status + index `as_of`), and `.to_dicts()` / `.to_pandas()` / `.to_polars()`.

Async is first-class:

```python
from ergon_tracker import AsyncErgonTracker, SearchQuery
async with AsyncErgonTracker() as et:
    res = await et.search(SearchQuery(keywords="data scientist", remote=True, limit=25))
```

### CLI

```bash
ergon-tracker search "engineer" --country Germany --level senior --remote --limit 20
ergon-tracker search "software engineer" --city "New York" --max-years 2 --strict-years \
  --salary-min 140000 --salary-currency USD --posted-within-days 30
ergon-tracker search "deep learning" --semantic                  # embedding-ranked
ergon-tracker search "backend" --visa-sponsor --sponsorship      # known H-1B sponsor + posting doesn't refuse
ergon-tracker sponsors "stripe"                                  # known H-1B sponsors + last-filed date
ergon-tracker resolve stripe.com                                 # -> {ats: greenhouse, token: stripe}
ergon-tracker sources                                            # every registered provider
ergon-tracker search "backend" --json | jq                       # machine-readable
```

### MCP (Claude / AI agents)

```bash
ergon-tracker-mcp     # stdio MCP server
```

Nine tools, pick by intent:

| Tool | Use it for |
|---|---|
| `search_jobs` | the workhorse — roles by keyword + typed filters (location, salary, level, years, degree, sector, remote, recency, visa) |
| `whats_new` | roles first-seen or updated in the last N days |
| `match_resume` | paste a résumé (or JD) → open roles ranked by fit (semantic) |
| `assess_fit` | one résumé vs one JD → structured gap analysis to tailor an application |
| `h1b_jobs` | open roles **at** known H-1B sponsors, ranked by sponsor strength |
| `list_h1b_sponsors` | "does *X* sponsor H-1B?" / the biggest sponsors |
| `resolve_company` | which ATS a company/URL uses + its board token |
| `list_sources` / `list_companies` | coverage introspection |

Client config (Claude Desktop / Claude Code): **[docs/mcp-quickstart.md](docs/mcp-quickstart.md)**.

## The prebuilt index (broad search, throttle-proof)

Broad queries (no `companies=`) are served from a **free daily SQLite/FTS5 snapshot** of every ATS
we track, published to a stable GitHub Release. The SDK downloads it once (cached under
`~/.cache/ergon-tracker`), verifies it (sha256 + schema version), and queries it **locally** — so
broad search is fast and makes **zero ATS requests at query time**.

- **Built by one CI crawler, not by users** — a tiered incremental crawl with per-host rate limiting
  and conditional requests (ETag → `304`); vector embeddings for semantic search are built by a
  parallel matrix job.
- **Auto-fresh** — each query checks the release manifest's `build_id` and pulls the newer snapshot
  (small row-level delta when possible). Every response carries `as_of` so freshness is visible.
- **Sector-sharded** — a `sector=` query pulls only that shard (a few MB).
- `ERGON_INDEX=off` forces everything live. Current coverage: **[INDEX_STATUS.md](INDEX_STATUS.md)**.

## Sources (30+)

Run `ergon-tracker sources` for the exact live list.

**Company ATS (20+):** Greenhouse · Lever · Ashby · Workday · SmartRecruiters · Workable · Recruitee ·
Personio · BambooHR · Breezy · Teamtailor · join.com · Rippling · Pinpoint · SuccessFactors · Oracle
Recruiting Cloud · Oracle Taleo · iCIMS · Eightfold · Avature · JazzHR · Phenom

**Aggregators (8):** RemoteOK · Remotive · Arbeitnow · Jobicy · Himalayas · TheMuse · **Adzuna**
(keyed) · **USAJOBS** (keyed)

ATS feeds are authoritative during dedup; aggregators broaden coverage. The enterprise ATSes
(SuccessFactors, Oracle, iCIMS, …) reach the large H-1B-sponsor employers smaller ATSes miss.

## Visa sponsorship

Two independent, deterministic signals, both on every job and filterable:

- **`visa_sponsor`** — is the *employer* a known H-1B sponsor? Matched against US DoL OFLC LCA
  certified filings, with **`visa_last_filed`** (most-recent filing) to tell active sponsors from
  quiet ones. *Positive evidence only* — absence ≠ "doesn't sponsor".
- **`sponsorship_offered`** — what the *posting* says: `True` (available), `False` (explicitly
  refused), or `None` (not stated — common; treat as unknown, not no).

```python
res = search("software engineer", visa_sponsor=True, sponsorship_offered=True, limit=20)
```

## Ranking

Filter (recall-first) → dedup → **field-weighted BM25** (title ≫ department/company ≫ description),
so ranking happens *before* the limit and the best matches survive. With `semantic=True`, embeddings
rerank the top candidates by meaning (local, CPU). A pluggable reranker seam lets a stronger
cross-encoder drop in later.

## Optional API keys (Adzuna & USAJOBS)

Copy `.env.example` → `.env` (gitignored) and fill in free keys; a missing key just **silently skips**
that source. Free keys: [developer.adzuna.com](https://developer.adzuna.com/),
[developer.usajobs.gov](https://developer.usajobs.gov/).

## Development

```bash
uv pip install -e ".[dev,mcp,semantic]"
pytest
ruff check src tests && ruff format --check src tests && mypy
```

## License

MIT — see [LICENSE](LICENSE).
