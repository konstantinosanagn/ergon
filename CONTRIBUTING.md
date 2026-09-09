# Contributing

## Setup

```bash
git clone https://github.com/konstantinosanagn/ergon-tracker
cd ergon-tracker
uv venv && uv pip install -e ".[dev,mcp,pandas,polars]"
pre-commit install
```

## The gates

CI runs these on Python 3.10–3.13. Run them before pushing:

```bash
ruff check src tests scripts
ruff format --check src tests scripts
mypy
pytest
```

`ruff` and `mypy` versions are pinned in `pyproject.toml` on purpose — a floating formatter
reformats differently across versions and turns CI red on code that was clean locally. Bump them
deliberately, never incidentally.

`mypy` covers `src/` only. `scripts/` needs roughly 470 annotations before it can be added.

## Crawling responsibly

This project fetches ~58,000 job boards across 54 third-party platforms. That access is a
privilege, and losing it breaks the project for everyone.

- Never raise a per-host rate limit to make a run finish faster.
- Never run a full crawl from a laptop. The daily crawl belongs in CI, where its budgets and
  circuit breakers are configured.
- New providers must go through `http.AsyncFetcher` so they inherit rate limiting, the circuit
  breaker, and the host budget.

## Adding a provider

1. Implement the `Provider` protocol in `providers/` (see `greenhouse.py` for the simplest example).
2. Register it in `providers/base.py`.
3. Add tests with `respx`-mocked responses. Do not add tests that hit the network — mark anything
   that must with `@pytest.mark.live` (skipped unless `ERGON_LIVE_TESTS=1`).
4. Read `providers/base.py` on the `fetch_detail` contract before implementing it. Returning
   `None` means "this posting is dead, expire the row". A transient failure must **raise**.
   Getting this backwards silently deletes live jobs from the index.

## Commits and PRs

Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `perf:`). The scope is the
subsystem, e.g. `fix(crawl):`. Explain *why* in the commit body; keep code comments to a line.

## Data and correctness

Extraction changes must keep the ratcheting gates in `tests/test_*_recall.py` passing. If a change
improves a metric, raise the gate to match. Never lower a gate to make CI green — the corpora exist
to make regressions loud.
