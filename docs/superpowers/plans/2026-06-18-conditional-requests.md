# Cross-Build Conditional Requests Implementation Plan

> **STATUS: COMPLETE (2026-06-18).** All tasks implemented + tested (greenhouse/lever/ashby
> opted in; smartrecruiters deferred — paginated). Remaining optimization noted at the end.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or
> superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Skip re-downloading unchanged ATS boards across daily builds by sending stored
`ETag`/`Last-Modified` validators and carrying forward on `304 Not Modified`.

**Architecture:** Capture each board's response validators during the crawl, persist them in
`BoardState` (fields already exist), and on the next crawl do a cheap conditional GET first; a
304 means "unchanged" → carry forward without parsing. Opt-in per provider via a single optional
`conditional_url(token)`; conditional logic lives in the crawler, not in each provider.

**Tech Stack:** httpx (MockTransport for tests), anyio, existing AsyncFetcher/scheduler/build.

---

## Feasibility evidence (measured 2026-06-18, live endpoints)

All four largest providers (~78% of the 350k-job index) return validators AND honor conditionals:

| Provider | Validator | Conditional result |
| --- | --- | --- |
| greenhouse | `ETag` | `If-None-Match` → **304** ✅ |
| lever | `ETag` | `If-None-Match` → **304** ✅ |
| smartrecruiters | `ETag` | `If-None-Match` → **304** ✅ |
| ashby | `Last-Modified` | `If-Modified-Since` → **304** ✅ |

recruitee returned no validator (`must-revalidate` only) — it simply won't opt in (no `conditional_url`), and falls back to a normal full fetch. That's the designed graceful path.

## Why this serves the loop goal

Steady-state daily builds re-crawl all "hot" boards. With conditionals, the majority that are
unchanged answer with a bodiless 304 — cutting bandwidth and effective load on the ATSes (more
polite = less throttling risk) while still publishing fresh data when boards do change.

---

### Task 1: `AsyncFetcher.conditional_get` — DONE ✅

Implemented + tested (`tests/test_http_conditional.py`, 4 cases): returns `ConditionalResult`
(`not_modified`/`status_code`/`etag`/`last_modified`/`body`); never raises or parses on 304;
sends `If-None-Match`/`If-Modified-Since` only when validators are provided.

### Task 2: Provider opt-in `conditional_url(token)`

**Files:** `src/ergon/providers/base.py` (optional protocol method, default None),
`greenhouse.py`, `lever.py`, `ashby.py`, `smartrecruiters.py`; tests in `tests/test_providers_*`.

- [ ] **Step 1:** Add to the `Provider` protocol an optional `def conditional_url(self, token) ->
  str | None: return None` (base default). Providers that support it return the single
  validatable list URL (the same URL their `fetch` hits first).
- [ ] **Step 2:** Implement for the 4 providers, returning the exact list endpoint:
  - greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs`
  - lever: `https://api.lever.co/v0/postings/{token}?mode=json`
  - ashby: `https://api.ashbyhq.com/posting-api/job-board/{token}` (note: ashby's fetch POSTs;
    the conditional GET on the same URL still returns Last-Modified — verify in the test)
  - smartrecruiters: `https://api.smartrecruiters.com/v1/companies/{token}/postings`
- [ ] **Step 3:** Unit-test each returns the expected URL; non-opted providers return None.

### Task 3: Crawler conditional pre-check (`scripts/build_index.py::_crawl_due`)

- [ ] **Step 1:** For each due board whose provider has a `conditional_url` AND whose `BoardState`
  has a stored `etag`/`last_modified`, call `fetcher.conditional_get(url, etag=..., last_modified=...)`.
- [ ] **Step 2:** On `not_modified`: record outcome `unchanged` (no jobs appended), do NOT call
  `provider.fetch`. On 200: store `res.etag`/`res.last_modified` into the board state, then call
  `provider.fetch` as today (accept one extra fetch on changed boards — the minority).
- [ ] **Step 3:** Boards with no `conditional_url` or no stored validator: unchanged behavior.
- [ ] **Step 4:** Thread the captured validators into `save_state` so they persist to the
  published `board_state.json` for the next build.

### Task 4: Scheduler `apply_outcome` — count 304 as a clean "unchanged" crawl

- [ ] **Step 1:** Ensure a 304 path marks `consecutive_unchanged += 1` and updates `last_crawled`
  without setting `last_changed` (so the board still ages hot→warm→cold normally).
- [ ] **Step 2:** Test: a board returning 304 for N days tiers down to warm/cold as expected.

### Task 5: End-to-end + metrics

- [ ] **Step 1:** Integration test with MockTransport: build state with a validator → 304 →
  carry-forward (row count stable, no re-parse) → history records a `not_modified_boards` count.
- [ ] **Step 2:** Add `not_modified_boards` to the build `history.jsonl` row + `INDEX_STATUS`.

## Self-review notes

- Double-fetch only on CHANGED boards (minority in steady state) — acceptable; documented.
- Validators are opaque strings echoed back verbatim (no parsing) → robust to weak/strong ETags.
- recruitee/others without validators are unaffected (graceful default-None path).

## Completion notes (2026-06-18)

- Task 1 ✅ `AsyncFetcher.conditional_get` + `ConditionalResult` (`tests/test_http_conditional.py`).
- Task 2 ✅ `Provider.conditional_url` (base default None) + greenhouse/lever/ashby overrides
  (`tests/test_provider_conditional_url.py`). smartrecruiters intentionally NOT opted in (paginated).
- Task 3 ✅ `_crawl_due` conditional pre-check (`tests/test_crawl_conditional.py`): 304 → carry
  forward, no `provider.fetch`; 200 → refresh validator + fetch. Validators persist via `save_state`.
- Task 4 ✅ scheduler ages 304 boards (`changed=False`) hot→warm→cold
  (`test_repeated_unchanged_ages_board_and_lengthens_interval`).
- Task 5 ✅ `not_modified_boards` in `history.jsonl`.
- **Live-proven:** unchanged greenhouse/lever/ashby boards return 304 with 0 bytes vs 1–3.6MB.

### Remaining optimization (future, optional)

On a 200 (changed/first crawl) the board is fetched twice (conditional GET downloads the body,
then `provider.fetch` re-downloads). Eliminate by adding `parse_body(token, body)` to the 3
opted-in providers and having the crawler parse `res.body` directly. Only matters for the
changed minority + one-time first crawl; steady-state unchanged boards already cost one cheap 304.
