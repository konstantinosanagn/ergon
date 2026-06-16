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
| **[Babak-hasani/company-career-scraper](https://github.com/Babak-hasani/company-career-scraper)** | Python | Free | Yes | Queries Greenhouse/Lever/Ashby/SmartRecruiters APIs directly. "No browser, $0 cost." Closest to jobspine's free-only philosophy. |
| [YvetteZheng0812/ats-job-scraper](https://github.com/YvetteZheng0812/ats-job-scraper) | Python | Free | Yes | 7 ATS (Ashby, Greenhouse, Lever, SmartRecruiters, Workable, Rippling, Workday) + SerpAPI company discovery. |
| [stevencc92/food-industry-job-scraper](https://github.com/stevencc92/food-industry-job-scraper) | Python | Free | Yes | Pipeline monitoring 5 ATS platforms to surface jobs "before they reach aggregators." |
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
