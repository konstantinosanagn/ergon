# Landscape: Open-Source Tools for Discovering & Fetching Live Job Postings

> Competitive / landscape intelligence for **jobspine** — a unified, free-only Python job-fetching SDK.
>
> **Snapshot date:** 2026-06-16
> **Method:** GitHub repository search (via GitHub API) + a deep-research pass (5 search angles, 17 sources fetched, 76 claims extracted, 25 adversarially verified with 3-vote refutation). Two scale claims were refuted in verification and are flagged below.
>
> **Caveats:** The GitHub search API used here does not return star counts, so projects are **not ranked by popularity** — qualitative prominence is noted instead. Scale/coverage numbers are largely self-reported by each project and should be treated skeptically.

---

## TL;DR for jobspine

1. **The free, ATS-API-direct, unified-SDK lane is real but uncrowded.** `JobSpy` dominates *board scraping*; nobody clearly owns *ATS-API aggregation as a clean, maintained, free Python SDK*. That gap is jobspine's wedge.
2. **MCP is the fastest-growing distribution surface.** A thin `jobspine-mcp` wrapper is cheap to ship and lands where the JobSpy ecosystem already proved demand.
3. **Free-API aggregators converge on the same providers jobspine uses** (Remotive, Arbeitnow, Adzuna, Jooble, JSearch, USAJOBS) — validating provider choices. Differentiation comes from **ATS-direct coverage + extraction quality**, not from chasing more free boards.
4. **Two sourcing strategies exist, with different durability:**
   - *Board scraping* (JobSpy): broad reach, fragile, proxy-dependent, ToS-gray.
   - *ATS-API direct* (jobspine's lane): narrower per-source but durable, clean JSON, no browser.

---

## 1. The heavyweight — the JobSpy ecosystem

The gravitational center of the entire space. Nearly everything else orbits it.

| Project | Stack | License | Free? | How it sources jobs |
|---|---|---|---|---|
| **[speedyapply/JobSpy](https://github.com/speedyapply/JobSpy)** (`python-jobspy` on PyPI) | Python | MIT | Yes | Scrapes LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter, Bayt, Naukri, BDJobs. Returns a pandas DataFrame. The most-forked job tool on GitHub. *(Org moved from `cullenwatson`/`Bunsly` → `speedyapply`.)* |
| [borgius/jobspy-mcp-server](https://github.com/borgius/jobspy-mcp-server) | Node / MCP | Free | Yes | MCP wrapper exposing JobSpy to Claude/Cursor/etc. (Fork: `chinpeerapat/jobspy-mcp-server`.) |
| [rainmanjam/jobspy-api](https://github.com/rainmanjam/jobspy-api) | Python / Docker | Free | Yes | Dockerized REST API around JobSpy: API-key auth, rate limiting, proxy support. |
| [alpharomercoma/ts-jobspy](https://github.com/alpharomercoma/ts-jobspy) | TypeScript | Free | Yes | Full TypeScript rewrite of python-jobspy. |
| [Liohtml/RUSTJobSpy](https://github.com/Liohtml/RUSTJobSpy) | Rust | Free | Yes | Rust port, concurrent scraping (Indeed, LinkedIn, Glassdoor, Naukri, Bayt, Google, ZipRecruiter, BDJobs). |
| [gonnan/jobspy](https://github.com/gonnan/jobspy) | Python | Free | Yes | JobSpy clone + OpenAI relevance scoring. |

**Relevance to jobspine:** JobSpy owns board scraping. It is fragile (undocumented endpoints, needs proxies) and ToS-gray. jobspine's ATS-API-direct approach is a different, more durable strategy.

---

## 2. ATS-direct fetchers — jobspine's closest competitors

These hit Greenhouse / Lever / Ashby / Workday / SmartRecruiters APIs directly rather than scraping boards. **This is jobspine's exact lane.**

| Project | Stack | License | Free? | How it sources jobs |
|---|---|---|---|---|
| **[kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers)** ("jobhive") | — | MIT | Yes | Claims ~47 ATS platforms scraped directly (Greenhouse, Lever, Ashby, Workday). Most direct conceptual competitor found. |
| **[Babak-hasani/company-career-scraper](https://github.com/Babak-hasani/company-career-scraper)** | Python | Free* | Yes (APIs) | **4** ATS (GH, Lever, Ashby, SmartRecruiters — *no Workday*). Public no-auth JSON. *Standout:* keyless **brute-force token discovery** from a company name. *Caveat:* main scraper **requires Google Sheets + service-account creds**; serial `requests`, no retries. See §9. |
| [YvetteZheng0812/ats-job-scraper](https://github.com/YvetteZheng0812/ats-job-scraper) | Python | **Partial** | APIs free; discovery paid | **7** ATS incl. Workday + **Rippling** (`__NEXT_DATA__`). **SerpAPI discovery is not reliably free** (~147 searches/run > 100/mo free tier → ~$25/mo). Threaded, retries. Personal CLI, not an SDK. See §9. |
| [stevencc92/food-industry-job-scraper](https://github.com/stevencc92/food-industry-job-scraper) | Python | Scraper free; **agent paid** | Partial | **5** ATS, **~64 hardcoded** companies (no discovery). **Broken pagination** (Workday cap 20, SmartRecruiters cap 100). "Food" is cosmetic (analytics-role keywords). `agent.py` needs a **paid Anthropic key**. See §9. |
| [Ramcharan747/careerscout](https://github.com/Ramcharan747/careerscout) | Go + Rust | Free | Yes | Ambitious: "maps every company's hiring infra," detects 17 ATS platforms across 5.5M+ domains, extracts job APIs at scale. Most infrastructure-grade discovery project found. |
| [Moo4president/Web-Scraper](https://github.com/Moo4president/Web-Scraper) | Python / Playwright | Free | Yes | Multi-ATS (Workday, Greenhouse, Lever…) into a consistent format; pagination + dedup. |
| [ghiarishi/job-scraper](https://github.com/ghiarishi/job-scraper) | — | Free | Yes | ATS-endpoint scraper. |
| [adgramigna/job-board-scraper](https://github.com/adgramigna/job-board-scraper) | — | Free | Yes | Job-board / ATS scraper. |
| [sm7/job-search](https://github.com/sm7/job-search) | Claude Agent skill | Free | Yes | Agent skill: finds/verifies/scores SWE jobs across Greenhouse/Ashby/Lever/Workday APIs. |
| [benmonopoli/open-greenhouse-mcp](https://github.com/benmonopoli/open-greenhouse-mcp) | MCP | Free | Yes | MCP server specifically for Greenhouse job boards. |

---

## 3. MCP servers — the AI-agent surface

Fast-growing category: agents that query live jobs through the Model Context Protocol.

| Project | Source / platform | Free? | Notes |
|---|---|---|---|
| [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) | LinkedIn | Yes | Most active LinkedIn MCP (profiles, companies, jobs, messages). |
| [eliasbiondo/linkedin-mcp-server](https://github.com/eliasbiondo/linkedin-mcp-server) · [Rayyan9477/linkedin_mcp](https://github.com/Rayyan9477/linkedin_mcp) · [Hritik003/linkedin-mcp](https://github.com/Hritik003/linkedin-mcp) | LinkedIn | Yes | Other LinkedIn MCP variants (search, scrape, apply). |
| [Himalayas-App/himalayas-mcp](https://github.com/Himalayas-App/himalayas-mcp) | Himalayas.app | Yes | **Official** MCP from a remote-jobs board — real listings + company info. |
| [kukapay/web3-jobs-mcp](https://github.com/kukapay/web3-jobs-mcp) | Web3 jobs | Yes | Real-time curated Web3 jobs. |
| [ChanMeng666/server-google-jobs](https://github.com/ChanMeng666/server-google-jobs) | Google Jobs (via SerpAPI) | **Server free (MIT), data freemium** | Code is free; depends on **SerpAPI** (free tier ~250 searches/mo, paid from $25/mo). Does **not** fit a strict free-only constraint. |
| [gmen1057/headhunter-mcp-server](https://github.com/gmen1057/headhunter-mcp-server) | HeadHunter / hh.ru | Yes | Search jobs, manage resumes, apply. |
| [wunderfrucht/jobsuche-mcp-server](https://github.com/wunderfrucht/jobsuche-mcp-server) | German Bundesagentur für Arbeit | Yes | Official German federal employment-agency data. |
| [aryaminus/h1b-job-search-mcp](https://github.com/aryaminus/h1b-job-search-mcp) | US DoL LCA disclosure data | Yes | Interesting **public-data** source (H-1B / Labor Condition Applications). |
| [6figr-com/jobgpt-mcp-server](https://github.com/6figr-com/jobgpt-mcp-server) · [MLS-Tech-Inc/shortlistjobs-mcp](https://github.com/MLS-Tech-Inc/shortlistjobs-mcp) · [0xDAEF0F/job-searchoor](https://github.com/0xDAEF0F/job-searchoor) | Various | Mixed | Search + auto-apply MCPs (some are front-ends to paid backends). |
| [vanooo/upwork-mcp](https://github.com/vanooo/upwork-mcp) · [zcrossoverz/upwork-mcp](https://github.com/zcrossoverz/upwork-mcp) | Upwork (gig) | Yes | Browser-automation MCPs for freelance jobs. |
| [FranRom/pupila](https://github.com/FranRom/pupila) | Local aggregator | Yes | Local-first daily aggregator + MCP server, BYO-LLM. |

---

## 4. Free-API aggregators — jobspine's "free-only" peers

Tools that stitch together free public job APIs. Directly relevant to jobspine's free-provider strategy.

| Project | Providers aggregated | License | Free? |
|---|---|---|---|
| [Federicohung/job-hub-api](https://github.com/Federicohung/job-hub-api) | Remotive, Arbeitnow, RemoteOK, JSearch, Adzuna, Jooble | Free | Yes |
| [AZaboobacker/ai_job_scanner](https://github.com/AZaboobacker/ai_job_scanner) | Adzuna, USAJOBS, Arbeitnow, Remotive, JSearch, Jooble (+ resume matching) | Free | Yes |
| [williamliu168/remote-ca-jobfinder](https://github.com/williamliu168/remote-ca-jobfinder) | 4 free public APIs, no keys, **rule-based (no LLM)** | Free | Yes — mirrors jobspine's deterministic-first principle |
| [itsgabh/job-hunt-toolkit](https://github.com/itsgabh/job-hunt-toolkit) | Free-API remote-jobs scraper (Claude plugin, 8 skills) | MIT | Yes |
| [Feashliaa/job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) | Aggregator | Free | Yes — ⚠️ its "1M+ jobs / 20K companies" claim was **REFUTED** in verification; treat scale claims skeptically |
| [spinov001-art/build-job-alert-bot](https://github.com/spinov001-art/build-job-alert-bot) | Free APIs → Telegram, ~47 lines | Free | Yes |

**Provider-overlap insight:** these projects converge on the same free providers jobspine already integrates. The competitive edge is not "more boards" — it's ATS-direct coverage and extraction quality.

---

## 5. Classic / long-standing

| Project | Stack | License | Free? | Notes |
|---|---|---|---|---|
| [PaulMcInnis/JobFunnel](https://github.com/PaulMcInnis/JobFunnel) | Python CLI | Free | Yes | Around since 2017. Scrapes boards into a dedup'd spreadsheet. Well-known. ⚠️ A specific claim about its scraping internals (Beautiful Soup / which boards) was **REFUTED 1-2** in verification — don't quote its mechanics without checking source. |
| [anatolykoptev/go-job](https://github.com/anatolykoptev/go-job) | Go | Free | Yes | Job tooling. |
| [Gsync/jobsync](https://github.com/Gsync/jobsync) | Full-stack | Free | Yes | Job tracker / sync app. |

---

## 6. Commercial / paid (for contrast)

| Project | Type | Cost | Notes |
|---|---|---|---|
| **Fantastic.jobs** ([Apify actor + MCP](https://apify.com/fantastic-jobs/career-site-job-listing-api/api/mcp)) | Career-site job-listing API | **Paid** | Proprietary. Publishes a useful reference on which ATS platforms expose APIs: <https://fantastic.jobs/article/ats-with-api> |
| [unidevbox/ats-job-listings-aggregator](https://apify.com/unidevbox/ats-job-listings-aggregator/api/mcp) (Apify) | ATS aggregator actor | **Paid** | Commercial ATS aggregation. |
| **SerpAPI** (underpins Google Jobs MCPs) | SERP API | **Freemium** | Free tier ~250 searches/mo; paid from $25/mo. The standard paid workaround for Google Jobs (which has no free official API). |

---

## Sourcing-strategy reference: ATS platforms with usable APIs

Common ATS systems that expose job-listing endpoints (the surfaces ATS-direct tools target):

- **Greenhouse** — public job-board JSON API (widely used, no auth for public boards)
- **Lever** — public postings API
- **Ashby** — public job-board API
- **SmartRecruiters** — public postings API
- **Workable** — public API
- **Recruitee**, **Personio** — public endpoints
- **Workday** — per-tenant endpoints (harder, often needs discovery)
- **Rippling** — public job-board endpoints

Free job-board / aggregator APIs commonly integrated:
**Remotive, Arbeitnow, RemoteOK, Adzuna, Jooble, JSearch (RapidAPI), USAJOBS, Jobicy, Himalayas, The Muse, Remotive, Muse.** (No-cost or generous free tiers.)

---

## Key competitive takeaways for jobspine

1. **No dominant free, unified, maintained ATS-direct SDK exists.** The closest analogs (`ats-scrapers`/jobhive, `company-career-scraper`, `careerscout`) are single-author, niche, or infrastructure experiments. This is jobspine's clearest opening.
2. **Ship an MCP wrapper early** — proven demand, low effort, high distribution.
3. **Lead with ATS-direct durability + clean normalized schema + extraction quality**, not raw board count. JobSpy already wins on board breadth.
4. **Public-data sources are underused** — e.g., US DoL LCA/H-1B data, German Bundesagentur. Free, official, and rarely aggregated.

---

## 7. Deep dive: jobspine vs. jobhive vs. careerscout (code-level)

Read at the source level on 2026-06-16 (jobhive @ `b45c12a` 2026-05-30; careerscout @ its single commit 2026-03-14). All three chase the same goal — live ATS jobs without paying — but sit at three points on the **curate ↔ discover** and **library ↔ infrastructure** axes.

| | **jobspine** (ours) | **jobhive** (`kalil0321/ats-scrapers`) | **careerscout** (`Ramcharan747`) |
|---|---|---|---|
| Stack | Python, async (httpx/anyio) | Python, async + browser fallbacks | Go core + Rust replay + Python ML + C eBPF |
| License | MIT | MIT | MIT |
| Shape | Installable SDK, **live fetch at call time** | SDK **+ batch pipeline** publishing a dataset to R2/CDN | Distributed crawl **infrastructure** (Kafka/Postgres/Fargate) |
| Discovery | Curated `seed.json` (**~1,400** companies) + offline resolver + dormant HTML-signature probe | Curated CSVs (**~88,000** tenants, 26 ATSes) maintained by PRs | Automated: Majestic Million + crt.sh + Common Crawl → probe |
| ATS-direct scrapers | **8** (GH, Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee, Personio) | ~26 multi-tenant ATS + 7 big-tech + 4 govt + ~11 boards (**49 modules**) | **15** probers + dynamic XHR shape classifier |
| Fetch method | Public JSON/XML APIs only — **no browser, no auth, no keys** | Public APIs + **stealth browser** (Tesla/Meta) + TLS-impersonation | Headless Chrome to *discover* endpoints, then **raw HTTP replay** |
| Freshness | **Live at query time** | Snapshot dataset (as fresh as last pipeline run) | Scheduled re-fetch (+6h) / replay loop |
| Dedup | Cross-source, company-blocked, rapidfuzz ≥90, **provenance union** | 5-pass, rapidfuzz ≥90, ATS-priority survivor | DB upsert on `(source_id, external_id)` |
| Enrichment | Deterministic: level, comp, yoe, sector, geo gazetteer | Deterministic, narrow (remote-from-title, salary regex); LLM deferred | regex + optional **Gemini Flash** schema auto-detect |
| Maturity | v0.1, active, **working MCP server** | Active, **51 test files**, real published dataset | **Single commit**, no data, eBPF/ML/normalise **stubbed** |

### The core difference is the answer to "which boards do I query?"

- **jobspine — curate small, fetch live.** ~1,400 hand-picked companies, resolved offline, fetched fresh every call. Best *freshness* + *simplicity*; smallest *universe*.
- **jobhive — curate big, batch-publish.** ~88k tenant CSVs grown by PRs, scraped on a schedule, published as Parquet/CSV behind a CDN. The library **downloads a snapshot** — it does *not* fetch live. Best *coverage*; data is as fresh as the last run.
- **careerscout — discover automatically.** No curated lists: harvests domains from Common Crawl + certificate transparency + Majestic Million, then *probes* to detect ATS. Most *ambitious*, but a design/portfolio repo (single commit, marquee eBPF/ML features stubbed, "5.5M" unverifiable; actual = 15 probers, 0 committed data).

### Where each wins

**jobspine's genuine edges over both:**
1. **Truly live** — jobhive serves a snapshot; we fetch at query time. For "jobs open *right now*," we win.
2. **Cleanest consumption** — typed SDK + CLI + **working MCP server** with per-source health. jobhive has no MCP; careerscout has no client surface.
3. **Best enrichment** — our level/comp/yoe/sector/geo extractors dwarf jobhive's two rules and careerscout's regex.
4. **Cross-source merge with provenance union** — collapsing a Greenhouse posting + a RemoteOK re-list into one job, both sources attributed. Neither competitor does this.
5. **No browser, no proxies, no keys** — lowest operational burden; trivially embeddable.

**Where jobspine is behind (honestly):**
1. **Coverage: ~1,400 vs ~88,000 companies.** The gap that matters.
2. **No automated discovery wired in.** `aresolve()` (HTML-signature probe) exists but the engine never calls it; today growth = hand-editing `seed.json`.
3. **No bot-defense story.** Clean public APIs only. Workday-at-scale, Tesla, Meta, Akamai-fronted boards are walls jobhive solved with stealth browsers and careerscout with eBPF/replay.

### Three ideas worth borrowing (concept, not code — they're Go/pipeline-shaped)

1. **From jobhive — curated-CSV-as-data + CI publish.** One CSV per ATS, PRs add tenants, a GitHub Action verifies + publishes. This is how they reached 88k without a crawler, and it is the **highest-leverage, lowest-risk** way to grow `seed.json`. Optionally publish a snapshot dataset alongside the live SDK (best of both).
2. **From careerscout — the discover-once / replay-cheap split.** Browsers only (re)capture auth tokens; everything else is replayed as raw HTTP, and a 401/403 marks the record stale to trigger re-discovery. Validates wiring our dormant `aresolve()` into a periodic *registry-enrichment* job feeding the cheap live path.
3. **From careerscout — response-shape classification.** Score an endpoint by the *shape* of its JSON (job-list vs single-job vs paginated) + ATS-vocabulary field matching. Useful if we ever add generic/unknown-ATS detection.

**Positioning:** we don't compete with careerscout (an unoperated experiment). We compete with **jobhive**, on **freshness + clean SDK/MCP + enrichment (we win) vs. raw coverage (they win)**. The move is to close coverage with their CSV model while keeping our live-fetch + provenance-merge + deterministic-enrichment advantages.

---

## 8. Experiment: crt.sh certificate-transparency harvester (negative result)

**Hypothesis (from careerscout's README):** querying crt.sh for `%.{ats-domain}` enumerates company tenants for free, a cheap way to auto-grow the registry. We built it and measured it. **Verdict: crt.sh is a weak source for jobspine's providers — not the gap-closer it appeared to be.**

**What we built** (`scripts/harvest_crtsh.py`, 8 passing unit tests in `tests/test_harvest_crtsh.py`):
- Queries crt.sh JSON for **subdomain-tenant** ATSes only (Recruitee, Personio, Workday), where the tenant is in the cert host name. Pure, tested extraction (`parse_crtsh_hosts`, `extract_tenants`, `extract_workday_site`) + infra-subdomain blocklist + all-numeric rejection.
- For Workday, discovers the required `site` path segment by reading the careers-root page's embedded `/wday/cxs/{tenant}/{site}/` reference (precise, not brute-force).
- **Proposes only** — emits a `candidates.json` consumed by the existing `build_registry.py`, which *verifies every candidate live* through jobspine's own providers before merging. (We also hardened `build_registry.py`'s ATS-priority map to `.get(..., 99)` so new ATS types can't `KeyError` the sweep.)

**What we measured (2026-06-16):**

| ATS | crt.sh hosts | Tenant candidates | **Verified live** | Why |
|---|---:|---:|---:|---|
| Recruitee | 66 | 19 | **0** | crt.sh surfaces Recruitee's *own* infra subdomains (`landing`, `metabase`, `s3`, `tagging-server`…); real customer boards hide behind a **wildcard cert** (`*.recruitee.com`) and never appear in CT logs. |
| Personio | 1 | 0 | **0** | Single wildcard cert (`*.jobs.personio.de`) — zero per-tenant certs to enumerate. |
| Workday | 98 (degraded) | 4 | — | Best theoretical case (per-tenant certs), but crt.sh was **502-ing** during testing and never returned a full result set. |

**Three concrete reasons crt.sh underdelivers here:**
1. **Wildcard certs hide tenants.** Many SaaS ATSes serve all customers under one `*.domain` cert, so individual tenants are invisible to certificate transparency. Confirmed for Recruitee and Personio.
2. **crt.sh is unreliable.** Frequent `502`s, rate-limiting, and truncated responses — it was fully down for part of our testing. Not a dependable production source.
3. **Path-based ATSes aren't enumerable at all.** Greenhouse/Lever/Ashby/SmartRecruiters/Workable — where **jobspine's bulk coverage lives** — put the token in a URL *path*, not a subdomain, so crt.sh can't see them regardless.

**Status & recommendation:** the harvester is kept (correct, tested, graceful under failure; genuinely useful for Workday-class per-tenant-cert ATSes *when crt.sh is up*), but it is **not** the path to closing the coverage gap. Two reliable, keyless alternatives beat it:
1. **jobhive's curated-CSV + CI-verify model** (§7 idea #1) — reliable, covers the path-based ATSes that matter most, reuses our live-verification gate.
2. **Brute-force token-variation discovery** (discovered in §9 below, in `company-career-scraper`) — given a company *name*, generate slug variants and probe the ATS endpoints directly. **Free, no keys, and it works precisely for the path-based ATSes (GH/Lever/Ashby) crt.sh cannot enumerate.** This is the strongest match to jobspine's free-only philosophy and is the recommended next discovery experiment.

**Artifacts:** `scripts/harvest_crtsh.py` · `tests/test_harvest_crtsh.py` · hardened `scripts/build_registry.py`.

---

## 9. Code-level reads: three ATS-direct personal tools (verified 2026-06-16)

Cloned and read at the source level (not from README blurbs). **All three are single-commit personal tools, not SDKs** — none is a library competitor to jobspine, but each has one idea worth noting.

| | company-career-scraper | ats-job-scraper | food-industry-job-scraper |
|---|---|---|---|
| ATS count | **4** (GH, Lever, Ashby, SmartRecruiters) | **7** (+ Workday, **Rippling**, Workable) | **5** (GH, Lever, Ashby, SmartRecruiters, Workday) |
| Fetch | Public no-auth JSON | Public JSON + Rippling `__NEXT_DATA__` scrape | Public JSON + Workday HTML for descriptions |
| Discovery | **Brute-force token guessing** from company name (keyless) | **SerpAPI** Google search (paid at full scale) | **None** — ~64 hardcoded companies |
| Company source | **Google Sheets (required)** | Discovery cache / `--add-slug` | Static Python lists |
| Concurrency | None (serial `requests`, 1s sleeps) | ThreadPoolExecutor per-ATS + retries | None (serial, no retries) |
| Pagination | SmartRecruiters offset loop; others single-call | Proper offset loops incl. Workday/Rippling | **Broken** (Workday cap 20, SR cap 100) |
| Schema | 7 flat fields, no salary/level | `JobListing` dataclass, no salary/level | 9 flat fields, no salary/level |
| Enrichment | Deterministic keyword + Germany location gate | Deterministic scoring (title/tech/location weights) | Deterministic title-keyword; optional **paid LLM** (`agent.py`) |
| LOC / tests | ~1,460 / **0** | ~2,010 / **0** | ~2,160 / 11 (config-only, no network) |
| License | MIT | MIT | **none committed** |
| Truly free? | APIs yes; **needs Google creds** | APIs yes; **discovery ~$25/mo** | scraper yes; **agent needs Anthropic key** |

### Per-tool notes

- **company-career-scraper** — closest in *spirit* to jobspine (free, no-auth, API-direct), but operationally heavier (Google Sheets + service-account JSON mandatory, serial, no retries, detector logic duplicated across two files with divergent Ashby field names). **Its `generate_token_variations()` is the valuable artifact:** ~15 deterministic slug candidates (case folds, hyphenation, corporate-suffix stripping, CamelCase) probed against each ATS and validated by `job_count >= 1`.
- **ats-job-scraper** — the best-engineered of the three (threaded, retries/backoff, jittered politeness). Mirrors jobspine's exact **Workday `tenant|wdN|site` triple-token** scheme — independent convergence on the same design. Adds **Rippling** (jobspine lacks it) via `__NEXT_DATA__` extraction. Gated on **paid SerpAPI** for discovery; `apply_assistant.py` is a static HTML dashboard + tiny Flask tracker (no LLM, despite the name).
- **food-industry-job-scraper** — weakest engineering (serial, no retries, **broken pagination** that silently truncates results). The repo name is misleading: the role filter targets **analytics jobs**, and "food" is a non-functional company tag. Worth noting only for its **SQLite cross-run new-job detection** (`load_seen_jobs()` / `INSERT OR IGNORE`) and a clean **paid-LLM fit-scoring** bolt-on (`agent.py`, Claude Sonnet, ~$2.50/300 jobs).

### What jobspine should take from this batch

1. **Brute-force token-variation discovery (highest leverage).** It's the keyless, free discovery method that works for the path-based ATSes crt.sh can't reach. A jobspine `scripts/harvest_tokens.py` could take a company-name list (or domains), generate variants, probe GH/Lever/Ashby/SmartRecruiters concurrently via the existing `AsyncFetcher`, and emit `candidates.json` for `build_registry.py` to verify — same propose/verify seam as the crt.sh harvester, but far higher yield.
2. **Rippling** is a small, well-scoped provider gap (one more `BaseProvider`, `__NEXT_DATA__` JSON extraction).
3. **Convergent validation:** two independent projects landed on jobspine's Workday triple-token approach — confidence that the hard part is modeled correctly.

**Net:** none of these three threatens jobspine as a library — they're personal scripts with no packaging, no tests of substance, no concurrency story (except ats-job-scraper's threads), thin schemas, and paid/credentialed dependencies. The one durable takeaway is the **brute-force token discovery technique.**

---

## Sources

Primary sources verified during research:

- <https://github.com/speedyapply/JobSpy> · <https://pypi.org/project/python-jobspy/>
- <https://github.com/borgius/jobspy-mcp-server> · <https://github.com/alpharomercoma/ts-jobspy>
- <https://github.com/PaulMcInnis/JobFunnel>
- <https://github.com/kalil0321/ats-scrapers> · <https://github.com/Feashliaa/job-board-aggregator>
- <https://github.com/anatolykoptev/go-job> · <https://github.com/adgramigna/job-board-scraper>
- <https://github.com/Gsync/jobsync> · <https://github.com/ghiarishi/job-scraper>
- <https://github.com/benmonopoli/open-greenhouse-mcp>
- <https://github.com/topics/job-aggregator>
- <https://apify.com/fantastic-jobs/career-site-job-listing-api/api/mcp> · <https://fantastic.jobs/article/ats-with-api>
- <https://apify.com/unidevbox/ats-job-listings-aggregator/api/mcp>
- SerpAPI pricing: <https://serpapi.com/pricing>

**Refuted claims (excluded from findings):**
- "job-board-aggregator indexes 1,000,000+ active jobs across 20,000+ companies" — refuted 1-2.
- "JobFunnel sources via Beautiful Soup HTML scraping of Indeed/Glassdoor/LinkedIn" — refuted 1-2 (mechanics uncertain; verify before citing).
