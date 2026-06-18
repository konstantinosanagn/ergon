# Search Index — build & dogfood log

Per-run notes for the broad-discovery search index (spec:
`docs/superpowers/specs/2026-06-18-search-index-design.md`; M1 plan:
`docs/superpowers/plans/2026-06-18-search-index-m1.md`).

## 2026-06-18 — M1 (pipeline proof) complete + live dogfood

**Status:** M1 done — all 12 tasks implemented TDD, full suite green (683+ tests), ruff clean
on all index files. Pipeline proven end-to-end on **live data**.

**Live build** (`scripts/build_index.py --limit-companies 60`):
- **18,469 jobs** crawled from 60 registry boards → `dist/index.sqlite` (28 MB raw / 3.8K… wait
  the gz of the full 28MB ≈ a few MB; small-tier number), `integrity_check = ok`.
- 60 companies; 3,814 jobs with salary; 10,242 with a known (non-unknown) level; 5,685 FTS
  matches for "engineer".

**Dogfood through the router/SDK** (broad query, served from the local index, zero ATS contact):
- `try_index(SearchQuery(keywords="senior backend engineer", level=SENIOR, limit=8))`
  → **2 ms**, 8 results, all genuinely-senior backend roles (Adobe / NVIDIA / Salesforce),
  ranked by FTS bm25. Confirms the core goal: **queryable data with no throttling at query time.**

**Bug found & fixed during dogfood:** the first build returned 0 jobs — `_crawl` passed registry
*slugs* to `companies=`, but `resolve()` expects domains/URLs. Rewrote `_crawl` to iterate
registry entries directly by their stored `(ats, token)`, crash-isolated per board. (This is
exactly why the working-style memory mandates dogfooding through the real tools — the unit tests
were all green while the live path was broken.)

**Next:** M2 (smart-tiered incremental crawl + conditional requests + throttle back-pressure +
GitHub Action) and M3 (observability + data-quality gates) complete v1. Then v2 = Approach B
(sector-sharded index + deltas) for optimized search.
