# Consolidation: Streams B / C / D — launch-readiness status (2026-06-22)

Three parallel workstreams executed alongside Stream A (coverage discovery, owned by a separate agent).
This ties them together and records the production-grade verification that backs "done." Stream A is
the only `seed.json` writer; B/C/D touch the built index, providers, extractors, MCP, and `pyproject` —
**zero `seed.json` collision** by construction.

Maps onto the publish roadmap: **exhaust ATSes (A) → crack the proven residual offline (B) → harden +
differentiate (C/D) → publish → JobSpy last.**

---

## Stream B — gated, offline-only browser discovery (structurally complete)

The browser never runs in the request path; it runs only on the offline build/discovery crons and
produces cheap artifacts the existing headless paths replay. Gated behind the ATS-exhaustion ladder
(consumes only `browser_queue.json`).

| Tier | What | Key files | Status |
|---|---|---|---|
| **1** | discover-once → apicapture spec → replay → verify | `scripts/browser_discovery.py` (pure `propose_spec` + in-memory `verify_spec`) | built; validated live vs `amazon.jobs` (proposed `records_path`/pagination match the hand-built spec; replays → real jobs) |
| **2** | JS-minted token: mint offline → cache (TTL) → inject → refresh | `token_store.py`, `apicapture` `token_ref`/`token_inject`, `scripts/token_mint.py` | built; mint driver validated live (cookie/storage/XHR capture → extract → store) |
| **cron** | TTL refresh (pre-crawl) + spec self-healing (post-build) | `scripts/tier2_refresh.py`, `spec_health.py`, `scripts/spec_health_cron.py`, `build-index.yml` | wired; `spec_health.json` persisted across builds so the failure streak survives |

**Remaining (gated):** per-site `tier2_mint.json` extract rules validated live against the real
proven-exhausted targets (Fastenal Akamai `_abck`, Sempra ADP-RM `myjobstoken`) — supervised, those
sites tarpit; and Tier-1 live runs once A emits a registry-wide `browser_queue.json`.

## Stream C — index quality (resolved honestly)

Diagnosed the index's high `level=unknown` against production data: it's **mostly correct-by-convention,
not a bug.** The one precision-safe lever (years→level) is already wired (`_relevel_from_years`) and
tested; the snippet lever is a measured dead end (1/8227); the remaining ~77% is unmarked source data
the labeling guide says *must* be `unknown`. **No title-guessing rules added** (would regress the locked
0.954). The real lever is crawl-policy (more descriptions) — Stream A's land. Documented in
`docs/extraction-baseline.md`.

## Stream D — distribution + the agent surface (publish-ready)

- **Packaging:** fixed the wheel double-include bug; `pip install ergon-tracker[mcp,semantic]` verified
  in a clean venv (registry ships + loads, 53 providers, both console scripts). One-line `uvx` onboarding
  + publish runbook in `docs/mcp-quickstart.md`. Added the `[browser]` extra (Tier-2 mint, offline-only).
- **MCP surface — 9 tools**, several with no competitor equivalent:
  `search_jobs` · **`whats_new`** (change feed — first-seen/updated since N days) · **`match_resume`**
  (embedding fit to a résumé/JD) · **`assess_fit`** (deterministic résumé↔JD gap analysis +
  `extract/skills.py` gazetteer) · **`h1b_jobs`** (LCA filing volume + recency joined to live jobs,
  ranked by sponsor strength) · `list_h1b_sponsors` · `resolve_company` · `list_sources` · `list_companies`.

**Remaining (gated on you):** `twine upload` (PyPI token).

---

## Production-readiness verification (run 2026-06-22)

All four CI gates + packaging + live integration, green:

| Check | Result |
|---|---|
| `ruff check src tests` | PASS |
| `ruff format --check src tests` | PASS (+ scripts linted to the same bar) |
| `mypy` | PASS (99 source files) |
| `pytest` | **1138 passed, 1 skipped** |
| `python -m build` + `twine check` | sdist + wheel PASS |
| clean-venv install + 9-tool registration | PASS |
| live D surface vs real index | `whats_new`/`h1b_jobs`(903-filing join)/`match_resume`(live embed)/`assess_fit` all OK |
| full B chain end-to-end | Tier-1 propose→verify (10 real jobs) · Tier-2 inject + mint roundtrip · self-healing streak |

The static pass caught + fixed 31 real issues (lint/format/type) in the new B/C/D code before they
reached CI — the point of the stress test.

## Launch checklist (when A's coverage plateaus + you're ready)

1. A's registry-wide sweep → `browser_queue.json` feeds B's Tier-1 live runs.
2. Validate Tier-2 per-site extract rules (supervised) → pair with `token_ref`/`token_inject` specs.
3. `twine upload` to PyPI; flip the `uvx` onboarding live.
4. (Post-launch) JobSpy board-scraping — isolated, opt-in, off-by-default.

Everything in B/C/D that does not depend on A's queue or your action is built, tested, and green.
