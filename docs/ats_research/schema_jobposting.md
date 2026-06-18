# Generic schema.org JobPosting (sitemap → JSON-LD) — "proxied giants" fetch contract

Mega-employers proxy their ATS server-side, so the ATS API is unreachable. But many publish
their public job data on their **own careers domain** as **XML sitemaps** of per-job URLs +
**schema.org `JobPosting` JSON-LD** on each detail page (so Google for Jobs can index them).
That public, structured surface is the way in — no auth, no browser, no API key.

The provider (`providers/schemaorg.py`) is **generic**: give it a careers host or a sitemap
URL, it resolves the job sitemap (robots.txt `Sitemap:` lines or common paths), walks any
nested `<sitemapindex>`, collects per-job detail URLs, fetches each, and parses the
`<script type="application/ld+json">` `JobPosting` via `BaseProvider.extract_jsonld_jobs`.

## The decisive distinction: server-rendered vs client-rendered JSON-LD

Google for Jobs' crawler renders JavaScript, so **many** big career sites inject the
`JobPosting` JSON-LD **client-side** — invisible to a no-browser fetch. A giant is only
crackable this way if the JSON-LD is **server-rendered** (present in the raw HTML). This was
the make-or-break test for every domain below.

## Live results (curl, UA `Mozilla/5.0 (compatible; ergon_tracker bot)`, 2026-06-18)

| Domain | Job sitemap? | Per-job JSON-LD server-rendered? | Verdict |
| --- | --- | --- | --- |
| `jobs.cvshealth.com` | ✅ `/us/en/sitemap_index.xml` → `sitemap{1..N}.xml` (777+ jobs/doc) | ✅ **`@type:JobPosting`** | **CRACKABLE** |
| `talent.lowes.com` | ✅ `/us/en/sitemap_index.xml` → `sitemap{1..7}.xml` (500 jobs/doc) | ✅ **`@type:JobPosting`** | **CRACKABLE** |
| `corporate.target.com` | ✅ robots → `/sitemapjob.xml` (`/jobs/...` URLs) | ❌ only `@type:WebPage` server-side | sitemap-only |
| `jobs.disneycareers.com` | ✅ `/sitemap-jobs.xml` (784 `/job/...` URLs) | ❌ JSON-LD injected client-side (Phenom SPA) | sitemap-only |
| `careers.walmart.com` | ✅ robots → `/sitemap.xml` (14,863 `/us/en/jobs/R-*`) | ❌ no JSON-LD; embedded AEM/Workday JSON blob (`jobPostingId`…) | sitemap-only |
| `www.amazon.jobs` | ⚠️ (React app) — but clean public `/en/search.json` API | ❌ no JSON-LD (React-rendered) | closed to JSON-LD (use JSON API instead) |
| `careers.google.com` | ⚠️ `/sitemap.xml` → `sitemap.txt` is static pages only; `/jobs/sitemap` empty | ❌ jobs load via XHR | closed |
| `jobs.apple.com` | ❌ `/robots.txt` 301→pagenotfound; API `POST /api/role/search` 301 | ❌ React app | closed |
| `careers.microsoft.com` | ❌ no robots sitemap (SPA `jobs.careers.microsoft.com`) | ❌ React app | closed |
| `careers.ibm.com` | ⚠️ `/careers/sitemap_index.xml` exists but `en_US` child is **0 bytes** | ❌ Phenom SPA | closed (empty sitemap) |
| `www.jpmorganchase.com/careers` | ⚠️ `careers.jpmorgan.com/sitemap.xml` is an index but `us/en` child 301-redirects | ❌ | closed (redirect loop) |

### CVS Health — confirmed JobPosting JSON-LD (`jobs.cvshealth.com/us/en/job/R0870953/...`)

```
KEYS: ['@context','@type','datePosted','description','directApply','employmentType',
       'hiringOrganization','identifier','jobLocation','occupationalCategory','title','workHours']
  title:        "Inventory Control Coordinator"
  datePosted:   "2026-04-05"
  employmentType: ["FULL_TIME"]                       # note: a LIST
  identifier:   {"@type":"PropertyValue","name":"CVS Health","value":"R0870953"}
  hiringOrganization: {"@type":"Organization","name":"CVS Health","url":".../job/R0870953/..."}
  jobLocation:  {"@type":"Place","geo":{...},"address":{"@type":"PostalAddress",
                 "postalCode":"66219","addressCountry":"United States",
                 "addressLocality":"Lenexa","addressRegion":"Kansas"}}
  description:  <7466 chars of HTML>
  validThrough: null   baseSalary: null   url: null
```

### Lowe's — identical shape (`talent.lowes.com/us/en/job/JR-02537670/...`)

```
  title:        "Sales Floor Dept Supervisor - Electrical - Plumbing"
  datePosted:   "2026-06-07"
  employmentType: ["FULL_TIME"]
  identifier:   {"@type":"PropertyValue","name":"Lowes","value":"JR-02537670"}
  jobLocation.address: {addressLocality:"Anchorage", addressRegion:"Alaska",
                        addressCountry:"United States of America", postalCode:"99504"}
```

Both are **Phenom-People** career sites that happen to server-render the JSON-LD (Disney, also
Phenom, does **not** — so the platform isn't a reliable signal; server-rendering must be
verified per tenant). The two crackable tenants emit the *exact same* JSON-LD shape, so one
generic parser covers both.

## Reliable JSON-LD fields (across the crackable tenants)

- `title` — always present (string).
- `datePosted` — `YYYY-MM-DD` → `posted_at`.
- `employmentType` — a **list** (`["FULL_TIME"]`) or string; mapped to `EmploymentType`.
- `identifier` — `PropertyValue` with `value` = req id → `source_job_id` (fallback: detail URL).
- `hiringOrganization.name` → `company` (fallback: derived from host).
- `jobLocation` — a dict **or** list of `Place`; `.address` → `Location(city/region/country)`.
- `description` — HTML → `description_html`.
- `validThrough` — present on some tenants but there is **no expiry field** in `JobPosting`, so
  it is intentionally **not** mapped.
- `baseSalary` / `url` — frequently `null`; mapped when present (`baseSalary.value.{min,max}Value`).

## Token shape

`schemaorg` is a **generic, opt-in** provider — it must not auto-claim arbitrary career hosts
during discovery (that would hijack every other provider), so `matches()` only resolves an
explicit `schemaorg:` / `schema:` scheme prefix and returns `None` otherwise. The token after
the prefix (or passed directly to `fetch`) is one of:

- a **careers host** — `"jobs.cvshealth.com"` (robots.txt / common paths are probed for the sitemap), or
- a **full sitemap URL** — `"https://talent.lowes.com/us/en/sitemap_index.xml"` (used directly).

## Bounds / behaviour

- Walks `<sitemapindex>` BFS, capped at `MAX_SITEMAP_DOCS` fetches.
- Detail-fetching is expensive, so it caps at `query.limit` (or a `DEFAULT_CAP` when unset,
  hard-capped at `HARD_CAP`), de-dups URLs, and honours `limit` early.
- Degrades to `[]` on any network error / 403 / gated sitemap; pages without a server-rendered
  `JobPosting` are simply skipped (so Walmart/Disney/Target yield `[]` gracefully, never crash).
- Missing fields normalize to `None` — never invented.
</content>
