# ergon-tracker

**A unified, free, reliable job-search engine in one Python package.** It fetches live
postings across **30+ sources** (company ATS feeds + aggregators), canonicalizes them into one
schema, **deduplicates the same job posted on many sites**, enriches each posting (level,
location, salary, years-of-experience, sector, **H-1B visa sponsorship**), and **ranks results
by relevance** — with an SDK, a CLI, and an **MCP server** so humans *and* AI agents can use it.

> Package name note: the project / repo / install name is **`ergon-tracker`**; the Python
> import is **`ergon_tracker`**; the commands are **`ergon-tracker`** and **`ergon-tracker-mcp`**.

---

## Why it exists

Every free job source speaks a different dialect, and the same role shows up on four sites. No
free OSS tool was *reliable + unified + deduped + ergonomic* at once. ergon-tracker is that tool:

- ✅ **Dedup is real.** The same posting from Greenhouse + RemoteOK + Adzuna collapses to **one**
  record — fuzzy title/company matching, location compatibility, the most authoritative source
  (employer ATS) wins, and every source that listed it is kept under `provenance`.
- ✅ **Live jobs are real.** Postings are fetched on demand, directly from source APIs.
- ✅ **Broad search without throttling.** A free **prebuilt index** (daily SQLite/FTS5 snapshot of
  every ATS we track) serves whole-registry queries locally — fast, anonymous, and with **zero ATS
  contact at query time**, so you (and everyone else) never get rate-limited. See below.
- ✅ **Filters are real and strong.** Level, location (country/city), salary range, years of
  experience, sector, remote, employment type, posting recency — all typed and tested.
- ✅ **Companies are easy to find & search.** Point at a domain (`stripe.com`) and it auto-detects
  the ATS and pulls that company's roles. A **46k-company** registry ships in the box.
- ✅ **Search by natural language.** Lexical **BM25** ranking by default (zero deps); optional
  **semantic** (embeddings) ranking for meaning/synonyms.
- ✅ **Visa-sponsorship aware.** Tags each job with whether the **employer is a known H-1B
  sponsor** (from US DoL LCA data, with the most-recent filing date) and whether the **posting
  itself** offers or refuses sponsorship — both filterable. Built for international applicants.
- ✅ **Agent-ready.** An MCP server exposes search / sponsors / resolve / list as tools, with
  relevance `score`s and structured fields, so an LLM can query it the way it expects.

Everything here is **free** — no paid APIs required. Two optional sources (Adzuna, USAJOBS) use
free API keys you provide.

---

## Install

Not on PyPI yet — install from the repo:

```bash
git clone https://github.com/konstantinosanagn/ergon-tracker
cd ergon-tracker

# with uv (recommended)
uv venv && uv pip install -e ".[mcp]"

# or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[mcp]"
```

Optional extras: `[mcp]` (agent server), `[semantic]` (NL embedding search),
`[pandas]` / `[polars]` (DataFrame export).

---

## Quickstart

### SDK (Python)

```python
from ergon_tracker import search

# Search a specific company's roles (auto-detects its ATS):
res = search("engineer", companies=["stripe.com"], limit=10)
for job in res.jobs:
    loc = job.locations[0].as_text() if job.locations else "—"
    print(f"{job.score:5.1f}  {job.title}  [{loc}]  ({job.source})")

# Strong filters, all combinable:
res = search(
    "backend",
    country="Germany",
    level="senior",
    salary_min=80000,
    remote=True,
    limit=20,
)

# Natural-language / semantic ranking (needs: pip install -e ".[semantic]"):
res = search("AI and deep learning roles at fintechs", semantic=True, limit=10)
```

`search()` returns a `SearchResult` with `.jobs` (ranked, each carrying a relevance `.score`),
`.health` (per-source status), and `.to_dicts()` / `.to_pandas()` / `.to_polars()`.

Async is first-class too:

```python
from ergon_tracker import AsyncErgonTracker, SearchQuery

async with AsyncErgonTracker() as et:
    res = await et.search(SearchQuery(keywords="data scientist", remote=True, limit=25))
```

### CLI

```bash
ergon-tracker search "engineer" --country Germany --level senior --remote --limit 20
ergon-tracker search "deep learning" --semantic            # embedding-ranked
ergon-tracker search "backend" --visa-sponsor --sponsorship # known H-1B sponsor + posting doesn't refuse
ergon-tracker sponsors "stripe"                             # browse known H-1B sponsors + last-filed date
ergon-tracker resolve stripe.com                            # -> {ats: greenhouse, token: stripe}
ergon-tracker sources                                       # list every registered provider
ergon-tracker search "backend" --json | jq                 # machine-readable output
```

### MCP (for Claude / AI agents)

```bash
# stdio MCP server exposing: search_jobs / list_h1b_sponsors / resolve_company / list_sources
ergon-tracker-mcp
```

See **[docs/mcp-quickstart.md](docs/mcp-quickstart.md)** for the Claude Desktop / Claude Code
config block.

---

## The prebuilt index (broad search, throttle-proof)

Two kinds of query, two paths — automatic, no config:

- **Targeted** (`companies=[...]` / specific sources) → fetched **live** from the source API
  (freshest, already fast).
- **Broad** (no company — searching the whole registry) → served from a **free prebuilt index**:
  a daily SQLite/FTS5 snapshot of every ATS we track, published to a stable GitHub Release. The
  SDK downloads it once (cached under `~/.cache/ergon-tracker`), verifies it, and queries it
  **locally**. That means broad search is **fast and makes zero ATS requests at query time**, so
  no one — you or the ATSes — ever gets rate-limited.

```python
from ergon_tracker import search
# No company -> served from the prebuilt index (instant, no ATS contact):
res = search("machine learning engineer", sector="AI/ML", remote=True, limit=20)
```

How it stays polite & current:
- **Built by one CI crawler**, not by users — a smart **tiered, incremental** crawl (hot/warm/cold
  boards) with per-host rate limiting and **conditional requests** (ETag/`If-None-Match` → `304`,
  so unchanged boards aren't re-downloaded). Users never crawl.
- **Anonymous** — no token needed (public release). **Integrity-gated** (sha256 + schema version +
  data-quality gates), so a bad build never replaces a good snapshot.
- **Sector-sharded** — a `sector=` query downloads only that shard (a few MB), not the whole index.

Controls:
- `ERGON_INDEX=off` — force everything live (skip the index entirely).
- Coverage of the current snapshot is in **[INDEX_STATUS.md](INDEX_STATUS.md)** (jobs by provider /
  sector / country, regenerated each build).

---

## Optional API keys (Adzuna & USAJOBS)

Two sources (Adzuna, USAJOBS) are free keyed search APIs. Add your keys to a `.env` file in the repo
root (gitignored — never committed). Copy `.env.example` to get started:

```bash
cp .env.example .env
# then fill in your keys
```

```bash
ADZUNA_APP_ID=...
ADZUNA_APP_KEY=...
USAJOBS_API_KEY=...
USAJOBS_EMAIL=...        # the email you registered with (sent as the required User-Agent)
```

If a key is missing, that source is **silently skipped** — it never breaks a search. Get free
keys at [developer.adzuna.com](https://developer.adzuna.com/) and
[developer.usajobs.gov](https://developer.usajobs.gov/).

---

## Sources (30+ and growing)

Run `ergon-tracker sources` for the live, exact list. Current coverage:

**Company ATS feeds (20+):** Greenhouse · Lever · Ashby · Workday · SmartRecruiters · Workable ·
Recruitee · Personio · BambooHR · Breezy · Teamtailor · join.com · Rippling · Pinpoint ·
SuccessFactors · Oracle Recruiting Cloud · Oracle Taleo · iCIMS · Eightfold · Avature · JazzHR

**Aggregators (8):** RemoteOK · Remotive · Arbeitnow · Jobicy · Himalayas · TheMuse ·
**Adzuna** (keyed) · **USAJOBS** (keyed)

ATS feeds are the authoritative source during dedup; aggregators broaden coverage. The
enterprise ATSes (SuccessFactors, Oracle, iCIMS, …) were added to reach the large H-1B-sponsor
employers (e.g. EY, SAP) that smaller ATSes miss.

---

## Visa sponsorship (for international applicants)

Two independent, deterministic signals — both surfaced on every job and filterable:

- **`visa_sponsor`** — is the *employer* a known H-1B sponsor? Matched against **76k employers**
  distilled from US DoL OFLC LCA certified filings (FY2025 + FY2026), with **`visa_last_filed`**
  (most-recent filing date) so you can tell active sponsors from ones that went quiet.
- **`sponsorship_offered`** — what the *posting text* says: `True` ("visa sponsorship available"),
  `False` ("must not require sponsorship now or in the future"), or `None` (not stated — common).

```python
# only known H-1B sponsors, and hide postings that explicitly refuse sponsorship:
res = search("software engineer", visa_sponsor=True, sponsorship_offered=True, limit=20)
for j in res.jobs:
    print(j.company, j.visa_sponsor, j.visa_last_filed, j.sponsorship_offered)
```

```bash
ergon-tracker sponsors            # biggest known H-1B sponsors + last-filed date
ergon-tracker sponsors "databricks"
```

Honesty note: `visa_sponsor` is *positive evidence only* (historical DoL data; absence ≠ "doesn't
sponsor"), and `sponsorship_offered` is regex over JD text (precise on explicit phrasing, but most
postings say nothing → `None`). Treat `None` as **unknown**, not no.

---

## How ranking works

1. **Filter** — each posting must pass your structured filters (and, in lexical mode, the
   keyword gate). Recall first: nothing relevant is dropped.
2. **Dedup** — cross-source duplicates merge into one record.
3. **Rank** — **field-weighted BM25** (title ≫ department/company ≫ description), so a search
   for "engineer" ranks *Engineer* roles above a sales role that merely mentions engineering.
   Every job gets a `.score`; ranking happens *before* the limit, so you keep the best matches.
4. **Semantic (opt-in)** — with `semantic=True`, embeddings rerank the top candidates by
   meaning (handles synonyms / NL intent). Runs locally on CPU via fastembed (~67 MB,
   already-quantized model); no API, no GPU. Tune with `ERGON_SEMANTIC_MODEL` /
   `ERGON_SEMANTIC_THREADS`.

A pluggable reranker seam means a stronger cross-encoder (e.g. ZeroEntropy `zerank`) can drop in
later as an extra — without touching the core.

---

## Development

```bash
uv pip install -e ".[dev,mcp,semantic]"
pytest          # full test suite
ruff check src tests && ruff format src tests
```

## License

MIT — see [LICENSE](LICENSE).
