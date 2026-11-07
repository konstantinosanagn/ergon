# Search Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A judged evaluation harness that gates four search-quality steps — query understanding into structured filters, title-alias expansion, a cross-encoder reranker, and (measured last) dense fusion — so each ships as a default only when it measurably wins on this corpus.

**Architecture:** Every step is a tri-state flag on `SearchQuery` with a package default written by the harness. `understand()` parses free text into the filters `SearchQuery` already has and is called once at the shared entry (`engine.run_search` and the MCP `search_jobs` tool, both of which feed `router.try_index_ranked`). Alias expansion lives in `query._query_match`. The reranker satisfies the existing `ranking.Reranker` protocol and plugs into `ranking.rank`. The harness is offline tooling under `scripts/search_eval/` with committed data under `data/search_eval/`.

**Tech Stack:** Python ≥3.10, pydantic models, SQLite FTS5, `ranx` (eval, dev-only), `fastembed` (ONNX runtime, `[rerank]`/`[semantic]` extras), `pyyaml` (already a dependency), O*NET 30.3 and GeoNames data (CC-BY 4.0).

**Spec:** `docs/superpowers/specs/2026-09-11-search-quality-design.md`

## Global Constraints

- `requires-python = ">=3.10"`; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (pinned mypy 2.3.1) must pass before every commit.
- The default install stays dependency-free beyond today's runtime deps; `ranx` is a **dev** dependency; `fastembed` only under the `[semantic]`/`[rerank]` extras; **no PyTorch in any install extra** (sentence-transformers is eval-only, installed ad hoc).
- Attribution: O*NET (CC-BY 4.0, "U.S. Department of Labor, Employment and Training Administration") and GeoNames (CC-BY 4.0) in a new `NOTICE` file, committed with the data they cover.
- A parsed value never overrides an explicit caller value; no extractor may raise on any input.
- Comments: one line inline at most; rationale goes in commit messages (repo rule).
- Commit messages: repo convention `type(scope): summary`; no AI-attribution trailers (repo rule).
- Every gate decision cites run ids in `data/search_eval/defaults.yaml`; a default flips only in a PR that includes the report.
- Tests import `scripts/` modules via `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))` (repo pattern); `pythonpath = ["."]` is set so `from tests.x import` also works.

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/search_eval/__init__.py` | package marker |
| `scripts/search_eval/queries.py` | load/validate `queries.yaml` → `list[Query]` |
| `scripts/search_eval/configs.py` | named run configurations → a `SearchQuery` transform + flags |
| `scripts/search_eval/run.py` | execute a config over the query set against a pinned index → TREC run file |
| `scripts/search_eval/pool.py` | union of top-20 across runs minus judged → `pool.jsonl` |
| `scripts/search_eval/judge.py` | expect-violation check, UMBRELA prompt, LLM drafting → `drafts.jsonl` |
| `scripts/search_eval/auditor.html` | adjudication UI (from `scripts/bench/label_auditor.html` pattern) |
| `scripts/search_eval/qrels.py` | merge adjudicated grades into `qrels.tsv` (TREC + `source`) |
| `scripts/search_eval/report.py` | metrics, per-slice, unjudged rate, paired test, CI half-widths, gate verdict, side-by-side HTML, `defaults.yaml` writer |
| `scripts/search_eval/__main__.py` | `python -m search_eval {run,pool,judge,qrels,report}` |
| `data/search_eval/queries.yaml` | the judged query set |
| `data/search_eval/qrels.tsv` | adjudicated relevance judgments |
| `data/search_eval/runs/*.trec` | committed run files (small) |
| `data/search_eval/defaults.yaml` | package defaults + justifying run ids |
| `src/ergon/query/__init__.py`, `understand.py`, `salary.py`, `experience.py`, `location.py`, `employment.py` | query understanding, one extractor per file |
| `data/query_places.yaml`, `scripts/build_query_places.py` | gazetteer + generator |
| `data/title_aliases.yaml`, `data/title_aliases.overrides.yaml`, `scripts/build_title_aliases.py` | alias table + generator |
| `src/ergon/query/aliases.py` | alias lookup + expansion variants |
| `src/ergon/rerank.py` | `Reranker` implementations (fastembed cross-encoder, late-interaction, eval-only ST) |
| `src/ergon/index/query.py`, `router.py`, `ranking.py`, `engine.py`, `mcp_server.py`, `cli.py`, `models.py` | wiring |
| `NOTICE` | data attributions |

---

## Phase A — Evaluation harness

### Task 1: Query set schema and loader

**Files:**
- Create: `scripts/search_eval/__init__.py` (empty), `scripts/search_eval/queries.py`, `data/search_eval/queries.yaml`
- Modify: `pyproject.toml` (add `"ranx>=0.3.20"` to `dev`)
- Test: `tests/search_eval/__init__.py` (empty), `tests/search_eval/test_queries.py`

**Interfaces:**
- Produces: `Query(id: str, text: str, slice: str, expect: dict[str, Any])`, `SLICES = ("constraint","title-synonym","seniority","exact-identifier","plain")`, `load_queries(path: Path) -> list[Query]` (raises `ValueError` on duplicate ids, unknown slice, or an `expect` key that is not a `SearchQuery` field).

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_queries.py
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.queries import SLICES, Query, load_queries  # noqa: E402


def _write(tmp_path, body: str) -> Path:
    p = tmp_path / "queries.yaml"
    p.write_text(body)
    return p


def test_loads_queries_with_expect(tmp_path):
    p = _write(tmp_path, """
- id: q1
  text: software engineer NYC >130k new grad
  slice: constraint
  expect: {city: New York, salary_min: 130000, max_years: 1}
- id: q2
  text: nurse practitioner Chicago
  slice: plain
""")
    qs = load_queries(p)
    assert qs == [
        Query("q1", "software engineer NYC >130k new grad", "constraint",
              {"city": "New York", "salary_min": 130000, "max_years": 1}),
        Query("q2", "nurse practitioner Chicago", "plain", {}),
    ]


@pytest.mark.parametrize("bad, msg", [
    ("- {id: q1, text: a, slice: plain}\n- {id: q1, text: b, slice: plain}", "duplicate id"),
    ("- {id: q1, text: a, slice: nope}", "unknown slice"),
    ("- {id: q1, text: a, slice: plain, expect: {colour: red}}", "not a SearchQuery field"),
])
def test_rejects_malformed(tmp_path, bad, msg):
    with pytest.raises(ValueError, match=msg):
        load_queries(_write(tmp_path, bad))


def test_slices_are_the_five_in_the_spec():
    assert SLICES == ("constraint", "title-synonym", "seniority", "exact-identifier", "plain")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_queries.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'search_eval'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/queries.py
"""The judged query set: `data/search_eval/queries.yaml`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ergon.models import SearchQuery

SLICES = ("constraint", "title-synonym", "seniority", "exact-identifier", "plain")
_FIELDS = set(SearchQuery.model_fields)


@dataclass(frozen=True)
class Query:
    id: str
    text: str
    slice: str
    expect: dict[str, Any] = field(default_factory=dict)


def load_queries(path: Path) -> list[Query]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    out: list[Query] = []
    seen: set[str] = set()
    for row in raw:
        qid = str(row["id"])
        if qid in seen:
            raise ValueError(f"duplicate id: {qid}")
        seen.add(qid)
        if row["slice"] not in SLICES:
            raise ValueError(f"unknown slice {row['slice']!r} on {qid}")
        expect = dict(row.get("expect") or {})
        bad = set(expect) - _FIELDS
        if bad:
            raise ValueError(f"{qid}: expect keys not a SearchQuery field: {sorted(bad)}")
        out.append(Query(qid, str(row["text"]), row["slice"], expect))
    return out
```

Add to `pyproject.toml` `dev` list: `"ranx>=0.3.20",` then `uv sync --all-extras`. Create `data/search_eval/queries.yaml` with the two queries from the test as the initial content (the full set is authored in Task 8).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_queries.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/__init__.py scripts/search_eval/queries.py data/search_eval/queries.yaml tests/search_eval pyproject.toml uv.lock
git commit -m "feat(search-eval): query set schema and loader"
```

---

### Task 2: Run configurations and the runner

**Files:**
- Create: `scripts/search_eval/configs.py`, `scripts/search_eval/run.py`
- Test: `tests/search_eval/test_run.py`

**Interfaces:**
- Consumes: `load_queries`, `Query` (Task 1); `ergon.index.query.search_rows(con, SearchQuery) -> list[sqlite3.Row]`; `ergon.index.db.connect(path, read_only=True)`.
- Produces: `CONFIGS: dict[str, Config]` where `Config(name: str, build: Callable[[str], SearchQuery])` maps query text to the `SearchQuery` this configuration runs; `run_config(name: str, index: Path, queries: list[Query], *, depth: int = 100) -> dict[str, list[tuple[str, float]]]` (query id → ranked `(job_id, score)`), `write_trec(run: dict, path: Path, name: str) -> None`, `index_build_id(index: Path) -> str`.

The `bm25` config is `SearchQuery(keywords=text, limit=depth)`; later tasks register `understand`, `aliases`, `rerank`, `dense` configs by setting their flags. Score for BM25 is the rank position inverted (`depth - rank`) because `search_rows` returns rows in `bm25()` order without exposing the score; TREC only needs a monotonic score.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_run.py
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.configs import CONFIGS  # noqa: E402
from search_eval.queries import Query  # noqa: E402
from search_eval.run import index_build_id, run_config, write_trec  # noqa: E402

from ergon.index.build import build_index
from ergon.models import JobPosting, Location


def _index(tmp_path):
    jobs = [
        JobPosting.create(source="greenhouse", source_job_id=f"j{i}", company=f"Co {i}", title=t)
        for i, t in enumerate(["Nurse Practitioner", "Software Engineer", "Clinical Nurse"])
    ]
    for j in jobs:
        j.locations = [Location(raw="Chicago, IL", city="Chicago", country="US")]
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="build-test-1")
    return p, jobs


def test_bm25_config_returns_ranked_ids(tmp_path):
    idx, jobs = _index(tmp_path)
    run = run_config("bm25", idx, [Query("q1", "nurse", "plain")], depth=10)
    ids = [jid for jid, _ in run["q1"]]
    assert set(ids) == {jobs[0].id, jobs[2].id}
    assert run["q1"][0][1] > run["q1"][1][1], "scores must be monotonic in rank"


def test_write_trec_format(tmp_path):
    out = tmp_path / "bm25.trec"
    write_trec({"q1": [("a", 2.0), ("b", 1.0)]}, out, "bm25")
    assert out.read_text().splitlines() == ["q1 Q0 a 1 2.0 bm25", "q1 Q0 b 2 1.0 bm25"]


def test_build_id_is_read_from_the_index(tmp_path):
    idx, _ = _index(tmp_path)
    assert index_build_id(idx) == "build-test-1"


def test_bm25_is_a_registered_config():
    assert "bm25" in CONFIGS and CONFIGS["bm25"].build("x").keywords == "x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_run.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'search_eval.configs'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/configs.py
"""Named run configurations. Each maps a query's text to the SearchQuery it runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ergon.models import SearchQuery


@dataclass(frozen=True)
class Config:
    name: str
    build: Callable[[str], SearchQuery]
    via_router: bool = False  # True: run through router.try_index_ranked (rerank/dense); False: search_rows


def _bm25(text: str) -> SearchQuery:
    return SearchQuery(keywords=text)


CONFIGS: dict[str, Config] = {"bm25": Config("bm25", _bm25)}
```

```python
# scripts/search_eval/run.py
"""Execute a configuration over the query set against a pinned index; write a TREC run."""

from __future__ import annotations

from pathlib import Path

from search_eval.configs import CONFIGS
from search_eval.queries import Query

from ergon.index.db import connect
from ergon.index.query import search_rows


def index_build_id(index: Path) -> str:
    con = connect(index, read_only=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='build_id'").fetchone()
    finally:
        con.close()
    return str(row[0]) if row else "unknown"


def run_config(
    name: str, index: Path, queries: list[Query], *, depth: int = 100
) -> dict[str, list[tuple[str, float]]]:
    cfg = CONFIGS[name]
    con = connect(index, read_only=True)
    try:
        out: dict[str, list[tuple[str, float]]] = {}
        for q in queries:
            sq = cfg.build(q.text).model_copy(update={"limit": depth})
            rows = search_rows(con, sq)
            out[q.id] = [(str(r["id"]), float(depth - i)) for i, r in enumerate(rows[:depth])]
        return out
    finally:
        con.close()


def write_trec(run: dict[str, list[tuple[str, float]]], path: Path, name: str) -> None:
    lines = [
        f"{qid} Q0 {jid} {rank} {score} {name}"
        for qid, ranked in run.items()
        for rank, (jid, score) in enumerate(ranked, 1)
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_run.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/configs.py scripts/search_eval/run.py tests/search_eval/test_run.py
git commit -m "feat(search-eval): run configurations and TREC runner"
```

---

### Task 3: Pooling

**Files:**
- Create: `scripts/search_eval/pool.py`
- Test: `tests/search_eval/test_pool.py`

**Interfaces:**
- Consumes: run dicts from Task 2; `qrels.tsv` format `qid\tjob_id\tgrade\tsource` (Task 5).
- Produces: `pool(runs: dict[str, dict[str, list[tuple[str, float]]]], judged: set[tuple[str, str]], *, top: int = 20) -> list[tuple[str, str]]` — sorted `(qid, job_id)` pairs present in any run's top-`top` and not yet judged; `read_judged(qrels: Path) -> set[tuple[str, str]]`; `write_pool(pairs, queries, index, out: Path) -> int` writing one JSON line per pair with `qid, text, expect, job_id, title, company, location, snippet` read from the index.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_pool.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.pool import pool, read_judged, write_pool  # noqa: E402
from search_eval.queries import Query  # noqa: E402

from ergon.index.build import build_index
from ergon.models import JobPosting


def test_pool_is_union_of_top_k_minus_judged():
    runs = {
        "a": {"q1": [("x", 3.0), ("y", 2.0), ("z", 1.0)]},
        "b": {"q1": [("w", 2.0), ("x", 1.0)]},
    }
    assert pool(runs, judged={("q1", "y")}, top=2) == [("q1", "w"), ("q1", "x")]


def test_read_judged(tmp_path):
    p = tmp_path / "qrels.tsv"
    p.write_text("q1\tx\t2\thuman\nq2\ty\t0\tllm\n")
    assert read_judged(p) == {("q1", "x"), ("q2", "y")}


def test_write_pool_carries_query_and_document_fields(tmp_path):
    j = JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="Nurse")
    idx = tmp_path / "i.sqlite"
    build_index([j], idx, build_id="b")
    out = tmp_path / "pool.jsonl"
    n = write_pool([("q1", j.id)], [Query("q1", "nurse", "plain", {"city": "X"})], idx, out)
    assert n == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["qid"] == "q1" and row["text"] == "nurse" and row["expect"] == {"city": "X"}
    assert row["job_id"] == j.id and row["title"] == "Nurse" and row["company"] == "Acme"
    assert set(row) >= {"location", "snippet"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_pool.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'search_eval.pool'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/pool.py
"""Judgment pool: every run's top-k, minus what is already graded."""

from __future__ import annotations

import json
from pathlib import Path

from search_eval.queries import Query

from ergon.index.db import connect


def read_judged(qrels: Path) -> set[tuple[str, str]]:
    if not qrels.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for line in qrels.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.add((parts[0], parts[1]))
    return out


def pool(
    runs: dict[str, dict[str, list[tuple[str, float]]]],
    judged: set[tuple[str, str]],
    *,
    top: int = 20,
) -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for run in runs.values():
        for qid, ranked in run.items():
            pairs.update((qid, jid) for jid, _ in ranked[:top])
    return sorted(pairs - judged)


def write_pool(
    pairs: list[tuple[str, str]], queries: list[Query], index: Path, out: Path
) -> int:
    by_q = {q.id: q for q in queries}
    con = connect(index, read_only=True)
    try:
        n = 0
        with out.open("w", encoding="utf-8") as fh:
            for qid, jid in pairs:
                row = con.execute(
                    "SELECT title, company, location, snippet FROM jobs WHERE id = ?", (jid,)
                ).fetchone()
                if row is None:
                    continue
                q = by_q[qid]
                fh.write(
                    json.dumps(
                        {
                            "qid": qid,
                            "text": q.text,
                            "expect": q.expect,
                            "job_id": jid,
                            "title": row[0],
                            "company": row[1],
                            "location": row[2],
                            "snippet": row[3],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                n += 1
        return n
    finally:
        con.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_pool.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/pool.py tests/search_eval/test_pool.py
git commit -m "feat(search-eval): judgment pooling"
```

---

### Task 4: Judging — expect check and UMBRELA drafting

**Files:**
- Create: `scripts/search_eval/judge.py`
- Test: `tests/search_eval/test_judge.py`

**Interfaces:**
- Consumes: pool rows (Task 3).
- Produces: `violates_expect(row: dict, con) -> str | None` (reason or None; reads the posting's `city`, `country`, `salary_min/max`, `years_min/max`, `level` from the index and compares to `expect` with `SearchQuery` semantics: `salary_min` expects posting `salary_max >= expect` or unknown-and-allowed, `max_years` expects posting `years_min <= expect`, `city`/`country` via `ergon.extract.geo.city_matches`/`country_matches`); `umbrela_prompt(query: str, expect: dict, doc: str) -> str`; `Grader` protocol `grade(prompt: str) -> int`; `AnthropicGrader` (model `claude-sonnet-5`, reads `ANTHROPIC_API_KEY`, parses `##final score: N`); `draft(pool: Path, index: Path, grader: Grader, out: Path) -> int` writing `drafts.jsonl` rows `{qid, job_id, grade, reason, source: "expect"|"llm"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_judge.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.judge import draft, umbrela_prompt, violates_expect  # noqa: E402

from ergon.index.build import build_index
from ergon.index.db import connect
from ergon.models import JobPosting, Location, Salary


def _idx(tmp_path):
    a = JobPosting.create(source="greenhouse", source_job_id="a", company="Acme", title="SWE")
    a.locations = [Location(raw="Boston, MA", city="Boston", country="US")]
    a.salary = Salary(min=100000, max=120000, currency="USD")
    b = JobPosting.create(source="greenhouse", source_job_id="b", company="Acme", title="SWE")
    b.locations = [Location(raw="New York, NY", city="New York", country="US")]
    b.salary = Salary(min=140000, max=160000, currency="USD")
    p = tmp_path / "i.sqlite"
    build_index([a, b], p, build_id="b")
    return p, a, b


def test_expect_violation_is_named(tmp_path):
    idx, a, b = _idx(tmp_path)
    con = connect(idx, read_only=True)
    exp = {"city": "New York", "salary_min": 130000}
    assert violates_expect({"job_id": a.id, "expect": exp}, con) is not None  # Boston, 120k
    assert violates_expect({"job_id": b.id, "expect": exp}, con) is None
    con.close()


def test_prompt_follows_umbrela_shape():
    p = umbrela_prompt("nurse Chicago", {"city": "Chicago"}, "Title: Nurse\nCompany: Acme")
    assert "0 to 3" in p and "##final score" in p
    assert "intent" in p.lower() and "constraint" in p.lower()
    assert "Query: nurse Chicago" in p and "Title: Nurse" in p


class _Fixed:
    def __init__(self, n: int) -> None:
        self.n, self.calls = n, 0

    def grade(self, prompt: str) -> int:
        self.calls += 1
        return self.n


def test_draft_writes_expect_zero_without_calling_the_llm(tmp_path):
    idx, a, b = _idx(tmp_path)
    pool = tmp_path / "pool.jsonl"
    exp = {"city": "New York", "salary_min": 130000}
    pool.write_text("\n".join(json.dumps({
        "qid": "q1", "text": "swe nyc >130k", "expect": exp, "job_id": j.id,
        "title": "SWE", "company": "Acme", "location": "", "snippet": "",
    }) for j in (a, b)) + "\n")
    g = _Fixed(3)
    out = tmp_path / "drafts.jsonl"
    assert draft(pool, idx, g, out) == 2
    rows = {r["job_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert rows[a.id]["grade"] == 0 and rows[a.id]["source"] == "expect"
    assert rows[b.id]["grade"] == 3 and rows[b.id]["source"] == "llm"
    assert g.calls == 1, "the LLM is only asked about pairs that pass the expect check"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_judge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'search_eval.judge'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/judge.py
"""Relevance drafting: deterministic expect check first, then an UMBRELA-style LLM grade."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol

from ergon.extract.geo import city_matches, country_matches
from ergon.index.db import connect

_PROMPT = """Given a query and a job posting, you must provide a score on an integer scale of 0 to 3 with the following meanings:
0 = the posting has nothing to do with the query,
1 = the posting seems related to the query but does not answer it,
2 = the posting has some answer for the query, but the answer may be a bit unclear, or hidden amongst extraneous information and
3 = the posting is dedicated to the query and contains the exact answer.

Important Instruction: Assign category 1 if the posting is somewhat related to the topic but not completely, category 2 if the posting presents something very important related to the entire topic but also has some extra information and category 3 if the posting only and entirely refers to the topic. If none of the above satisfies give it category 0.

Query: {query}
Hard constraints the posting must satisfy: {expect}
Posting:
{doc}

Split this problem into steps:
Consider the underlying intent of the search.
Measure how well the content matches a likely intent of the query (M).
Measure whether the posting satisfies every hard constraint (C).
Consider the aspects above and the relative importance of each, and decide on a final score (O). Final score must be an integer value only.
Do not provide any code in result. Provide each score in the format of: ##final score: score without providing any reasoning."""


def umbrela_prompt(query: str, expect: dict[str, Any], doc: str) -> str:
    return _PROMPT.format(query=query, expect=json.dumps(expect) if expect else "none", doc=doc)


def violates_expect(row: dict[str, Any], con: sqlite3.Connection) -> str | None:
    exp = row.get("expect") or {}
    if not exp:
        return None
    r = con.execute(
        "SELECT city, country, location, salary_min, salary_max, years_min, years_max, level "
        "FROM jobs WHERE id = ?",
        (row["job_id"],),
    ).fetchone()
    if r is None:
        return "posting missing from index"
    city, country, loc, smin, smax, ymin, ymax, level = r
    if "city" in exp and not city_matches(exp["city"], city, loc):
        return f"city {city!r} != {exp['city']!r}"
    if "country" in exp and not country_matches(exp["country"], country, loc):
        return f"country {country!r} != {exp['country']!r}"
    if "salary_min" in exp and smax is not None and smax < exp["salary_min"]:
        return f"salary_max {smax} < {exp['salary_min']}"
    if "salary_max" in exp and smin is not None and smin > exp["salary_max"]:
        return f"salary_min {smin} > {exp['salary_max']}"
    if "max_years" in exp and ymin is not None and ymin > exp["max_years"]:
        return f"years_min {ymin} > {exp['max_years']}"
    if "min_years" in exp and ymax is not None and ymax < exp["min_years"]:
        return f"years_max {ymax} < {exp['min_years']}"
    if "level" in exp and level not in (None, "unknown", exp["level"]):
        return f"level {level!r} != {exp['level']!r}"
    return None


class Grader(Protocol):
    def grade(self, prompt: str) -> int: ...


class AnthropicGrader:
    def __init__(self, model: str = "claude-sonnet-5") -> None:
        import anthropic  # offline tooling only; never on the runtime path

        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = model

    def grade(self, prompt: str) -> int:
        msg = self._client.messages.create(
            model=self._model, max_tokens=20, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
        m = re.search(r"##final score:\s*([0-3])", text)
        if not m:
            raise ValueError(f"unparseable grade: {text!r}")
        return int(m.group(1))


def _doc(row: dict[str, Any]) -> str:
    return (
        f"Title: {row.get('title') or ''}\nCompany: {row.get('company') or ''}\n"
        f"Location: {row.get('location') or ''}\nSnippet: {row.get('snippet') or ''}"
    )


def draft(pool: Path, index: Path, grader: Grader, out: Path) -> int:
    con = connect(index, read_only=True)
    try:
        n = 0
        with out.open("w", encoding="utf-8") as fh:
            for line in pool.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                reason = violates_expect(row, con)
                if reason is not None:
                    rec = {"qid": row["qid"], "job_id": row["job_id"], "grade": 0,
                           "reason": reason, "source": "expect"}
                else:
                    g = grader.grade(umbrela_prompt(row["text"], row.get("expect") or {}, _doc(row)))
                    rec = {"qid": row["qid"], "job_id": row["job_id"], "grade": g,
                           "reason": "", "source": "llm"}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        return n
    finally:
        con.close()
```

Add `"anthropic>=0.40"` to the `dev` dependency list (offline tooling only).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_judge.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/judge.py tests/search_eval/test_judge.py pyproject.toml uv.lock
git commit -m "feat(search-eval): expect check and UMBRELA-style draft grading"
```

---

### Task 5: Adjudication auditor and qrels merge

**Files:**
- Create: `scripts/search_eval/auditor.html`, `scripts/search_eval/qrels.py`
- Test: `tests/search_eval/test_qrels.py`

**Interfaces:**
- Consumes: `drafts.jsonl` (Task 4).
- Produces: `select_for_review(drafts: list[dict], *, zero_sample: float = 0.10, seed: int = 0) -> list[dict]` (every non-zero draft plus a deterministic 10% sample of zeros); `merge(drafts: Path, adjudicated: Path | None, qrels: Path) -> int` writing/appending `qid\tjob_id\tgrade\tsource` where an adjudicated row (`{qid, job_id, grade}`) overrides the draft with `source=human`, else the draft stands with `source=llm` (or `expect`); existing qrels rows are never changed.

The auditor is a copy of `scripts/bench/label_auditor.html` reduced to: load `review.jsonl` (the output of `select_for_review`, which carries the pool fields so the adjudicator sees the query, expect, title, company, location, snippet, the draft grade and reason), a 0/1/2/3 selector per row defaulting to the draft, localStorage persistence, and a "Download adjudicated.jsonl" button emitting `{qid, job_id, grade}` for rows the adjudicator changed **or confirmed**. No network calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_qrels.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.qrels import merge, select_for_review  # noqa: E402


def test_review_selection_shows_all_nonzero_and_a_tenth_of_zeros():
    drafts = [{"qid": "q", "job_id": f"j{i}", "grade": 0, "source": "llm"} for i in range(100)]
    drafts += [{"qid": "q", "job_id": "hit", "grade": 2, "source": "llm"}]
    sel = select_for_review(drafts, zero_sample=0.10, seed=1)
    assert any(r["job_id"] == "hit" for r in sel)
    assert 8 <= sum(1 for r in sel if r["grade"] == 0) <= 12
    assert select_for_review(drafts, seed=1) == sel, "the sample is deterministic"


def test_merge_prefers_human_and_never_rewrites_existing(tmp_path):
    d = tmp_path / "drafts.jsonl"
    d.write_text("\n".join(json.dumps(r) for r in [
        {"qid": "q", "job_id": "a", "grade": 1, "source": "llm"},
        {"qid": "q", "job_id": "b", "grade": 0, "source": "expect"},
        {"qid": "q", "job_id": "c", "grade": 3, "source": "llm"},
    ]) + "\n")
    adj = tmp_path / "adjudicated.jsonl"
    adj.write_text(json.dumps({"qid": "q", "job_id": "a", "grade": 3}) + "\n")
    q = tmp_path / "qrels.tsv"
    q.write_text("q\tc\t2\thuman\n")
    assert merge(d, adj, q) == 2
    assert q.read_text().splitlines() == ["q\tc\t2\thuman", "q\ta\t3\thuman", "q\tb\t0\texpect"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_qrels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'search_eval.qrels'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/qrels.py
"""Adjudication selection and the committed qrels file (TREC columns + a source column)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any


def select_for_review(
    drafts: list[dict[str, Any]], *, zero_sample: float = 0.10, seed: int = 0
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    zeros = [r for r in drafts if r["grade"] == 0]
    keep = set(id(r) for r in rng.sample(zeros, round(len(zeros) * zero_sample)))
    return [r for r in drafts if r["grade"] != 0 or id(r) in keep]


def merge(drafts: Path, adjudicated: Path | None, qrels: Path) -> int:
    existing: set[tuple[str, str]] = set()
    lines: list[str] = []
    if qrels.exists():
        lines = qrels.read_text(encoding="utf-8").splitlines()
        existing = {tuple(ln.split("\t")[:2]) for ln in lines if ln}
    human: dict[tuple[str, str], int] = {}
    if adjudicated and adjudicated.exists():
        for ln in adjudicated.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                human[(r["qid"], r["job_id"])] = int(r["grade"])
    added = 0
    for ln in drafts.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        key = (r["qid"], r["job_id"])
        if key in existing:
            continue
        if key in human:
            grade, source = human[key], "human"
        else:
            grade, source = int(r["grade"]), r["source"]
        lines.append(f"{key[0]}\t{key[1]}\t{grade}\t{source}")
        existing.add(key)
        added += 1
    qrels.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added
```

For `auditor.html`: copy `scripts/bench/label_auditor.html`, keep its file loader, localStorage persistence and download button, replace the per-row body with the fields above and a `<select>` of 0–3 whose default is the draft grade; the download emits `{qid, job_id, grade}` for every reviewed row. Manual check: open the file, load a `review.jsonl`, change one grade, download, confirm the JSON.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_qrels.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/qrels.py scripts/search_eval/auditor.html tests/search_eval/test_qrels.py
git commit -m "feat(search-eval): adjudication auditor and qrels merge"
```

---

### Task 6: Report — metrics, gate, side-by-side

**Files:**
- Create: `scripts/search_eval/report.py`
- Test: `tests/search_eval/test_report.py`

**Interfaces:**
- Consumes: TREC runs and qrels on disk; `ranx`.
- Produces: `load_qrels(path: Path) -> tuple[ranx.Qrels, ranx.Qrels]` (graded, and binarized at grade ≥ 2); `evaluate_run(run_path: Path, qrels_graded, qrels_binary, queries: list[Query]) -> RunReport` with fields `name`, `ndcg10: float`, `recall100: float`, `by_slice: dict[str, tuple[float, float]]`, `unjudged_rate: float` (share of top-10 pairs with no qrel), `per_query_ndcg: dict[str, float]`; `gate(candidate: RunReport, default: RunReport, qrels_graded, *, alpha: float = 0.05, n_tests: int = 1) -> GateResult(passed: bool, reasons: list[str], p_value: float)` applying the four rules from spec §7 (paired Student's t via `ranx.compare`, Bonferroni `alpha / n_tests`; per-slice tolerance = bootstrap 95% CI half-width of the default's per-query metric in that slice, 1,000 resamples, seed 0; unjudged rate < 0.10); `side_by_side(default: Path, candidate: Path, queries, index: Path, out: Path) -> None` writing an HTML with top-10 per query for both runs, grades shown, differences highlighted; `write_defaults(path: Path, step: str, enabled: bool, run_ids: list[str], report: RunReport) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_report.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.queries import Query  # noqa: E402
from search_eval.report import evaluate_run, gate, load_qrels  # noqa: E402
from search_eval.run import write_trec  # noqa: E402

QS = [Query("q1", "a", "plain"), Query("q2", "b", "exact-identifier")]


def _files(tmp_path):
    q = tmp_path / "qrels.tsv"
    q.write_text("q1\tx\t3\thuman\nq1\ty\t1\tllm\nq2\tz\t2\thuman\n")
    good = tmp_path / "good.trec"
    write_trec({"q1": [("x", 2.0), ("y", 1.0)], "q2": [("z", 1.0)]}, good, "good")
    bad = tmp_path / "bad.trec"
    write_trec({"q1": [("y", 2.0), ("x", 1.0)], "q2": [("u", 2.0), ("z", 1.0)]}, bad, "bad")
    return q, good, bad


def test_metrics_and_slices(tmp_path):
    q, good, bad = _files(tmp_path)
    g, b = load_qrels(q)
    rg = evaluate_run(good, g, b, QS)
    rb = evaluate_run(bad, g, b, QS)
    assert rg.ndcg10 == 1.0 and rg.recall100 == 1.0
    assert rb.ndcg10 < rg.ndcg10
    assert set(rg.by_slice) == {"plain", "exact-identifier"}
    assert rb.unjudged_rate > 0 and rg.unjudged_rate == 0  # 'u' was never judged


def test_gate_refuses_a_regression_and_reports_why(tmp_path):
    q, good, bad = _files(tmp_path)
    g, b = load_qrels(q)
    res = gate(evaluate_run(bad, g, b, QS), evaluate_run(good, g, b, QS), g)
    assert not res.passed
    assert any("exact-identifier" in r for r in res.reasons)


def test_gate_passes_identical_runs_only_on_significance(tmp_path):
    q, good, _ = _files(tmp_path)
    g, b = load_qrels(q)
    same = evaluate_run(good, g, b, QS)
    res = gate(same, same, g)
    assert not res.passed and any("significan" in r for r in res.reasons)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'search_eval.report'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/report.py
"""Metrics, the gate from spec §7, the side-by-side review page, and the defaults file."""

from __future__ import annotations

import html
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ranx import Qrels, Run, compare, evaluate

from search_eval.queries import Query

from ergon.index.db import connect


def load_qrels(path: Path) -> tuple[Qrels, Qrels]:
    graded: dict[str, dict[str, int]] = {}
    binary: dict[str, dict[str, int]] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        qid, jid, grade, _src = ln.split("\t")
        g = int(grade)
        graded.setdefault(qid, {})[jid] = g
        if g >= 2:
            binary.setdefault(qid, {})[jid] = 1
    return Qrels(graded), Qrels(binary)


@dataclass
class RunReport:
    name: str
    ndcg10: float
    recall100: float
    by_slice: dict[str, tuple[float, float]]
    unjudged_rate: float
    per_query_ndcg: dict[str, float]
    per_query_recall: dict[str, float] = field(default_factory=dict)
    run: Run | None = None


def _per_query(qrels: Qrels, run: Run, metric: str) -> dict[str, float]:
    evaluate(qrels, run, [metric])
    return dict(run.scores[metric])


def evaluate_run(run_path: Path, qrels_graded: Qrels, qrels_binary: Qrels, queries: list[Query]) -> RunReport:
    run = Run.from_file(str(run_path), kind="trec")
    run.name = run_path.stem
    nd = _per_query(qrels_graded, run, "ndcg@10")
    rc = _per_query(qrels_binary, run, "recall@100")
    slices: dict[str, list[str]] = {}
    for q in queries:
        slices.setdefault(q.slice, []).append(q.id)
    by_slice = {
        s: (
            sum(nd.get(q, 0.0) for q in ids) / len(ids),
            sum(rc.get(q, 0.0) for q in ids) / len(ids),
        )
        for s, ids in slices.items()
    }
    judged = {(q, d) for q, docs in qrels_graded.qrels.items() for d in docs}
    top10 = [(q, d) for q, docs in run.run.items() for d in list(docs)[:10]]
    unjudged = sum(1 for p in top10 if p not in judged) / max(1, len(top10))
    return RunReport(
        run.name,
        sum(nd.values()) / max(1, len(nd)),
        sum(rc.values()) / max(1, len(rc)),
        by_slice,
        unjudged,
        nd,
        rc,
        run,
    )


@dataclass
class GateResult:
    passed: bool
    reasons: list[str]
    p_value: float


def _half_width(values: list[float], *, n: int = 1000, seed: int = 0) -> float:
    if len(values) < 2:
        return 0.0
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(n))
    return (means[int(0.975 * n) - 1] - means[int(0.025 * n)]) / 2


def gate(
    candidate: RunReport, default: RunReport, qrels_graded: Qrels, *, alpha: float = 0.05, n_tests: int = 1
) -> GateResult:
    reasons: list[str] = []
    assert candidate.run is not None and default.run is not None
    rep = compare(qrels_graded, [default.run, candidate.run], ["ndcg@10"], stat_test="student", max_p=alpha / n_tests)
    p = float(rep.comparisons[("ndcg@10", default.run.name, candidate.run.name)]["p_value"]) if rep.comparisons else 1.0
    if candidate.ndcg10 <= default.ndcg10 or p >= alpha / n_tests:
        reasons.append(f"nDCG@10 gain not significant (p={p:.3f}, {default.ndcg10:.3f} -> {candidate.ndcg10:.3f})")
    for s, (nd, rc) in default.by_slice.items():
        ids = [q for q in default.per_query_ndcg if q in candidate.per_query_ndcg and s in s]
        tol_nd = _half_width([default.per_query_ndcg[q] for q in default.per_query_ndcg])
        tol_rc = _half_width([default.per_query_recall[q] for q in default.per_query_recall])
        cnd, crc = candidate.by_slice.get(s, (0.0, 0.0))
        if cnd < nd - tol_nd:
            reasons.append(f"slice {s}: nDCG@10 {nd:.3f} -> {cnd:.3f} regresses beyond tolerance {tol_nd:.3f}")
        if crc < rc - tol_rc:
            reasons.append(f"slice {s}: Recall@100 {rc:.3f} -> {crc:.3f} regresses beyond tolerance {tol_rc:.3f}")
    if candidate.unjudged_rate >= 0.10:
        reasons.append(f"unjudged rate {candidate.unjudged_rate:.0%} >= 10%: extend the pool and re-judge")
    return GateResult(not reasons, reasons, p)


def side_by_side(default: Path, candidate: Path, queries: list[Query], qrels: Path, index: Path, out: Path) -> None:
    graded, _ = load_qrels(qrels)
    runs = {p.stem: Run.from_file(str(p), kind="trec") for p in (default, candidate)}
    con = connect(index, read_only=True)
    try:
        def cell(qid: str, jid: str) -> str:
            row = con.execute("SELECT title, company, location FROM jobs WHERE id=?", (jid,)).fetchone()
            g = graded.qrels.get(qid, {}).get(jid, "·")
            t = " · ".join(html.escape(str(x or "")) for x in (row or ("?", "", "")))
            return f"<td class=g{g}>[{g}] {t}</td>"
        parts = ["<style>table{border-collapse:collapse;font:13px system-ui}td,th{border:1px solid #ccc;padding:3px 6px;vertical-align:top}.g3{background:#cfc}.g2{background:#efc}.g0{background:#fdd}</style>"]
        for q in queries:
            parts.append(f"<h3>{html.escape(q.id)} — {html.escape(q.text)} <small>({q.slice})</small></h3><table><tr><th>{default.stem}</th><th>{candidate.stem}</th></tr>")
            a = list(runs[default.stem].run.get(q.id, {}))[:10]
            b = list(runs[candidate.stem].run.get(q.id, {}))[:10]
            for i in range(max(len(a), len(b))):
                ca = cell(q.id, a[i]) if i < len(a) else "<td></td>"
                cb = cell(q.id, b[i]) if i < len(b) else "<td></td>"
                parts.append(f"<tr>{ca}{cb}</tr>")
            parts.append("</table>")
        out.write_text("\n".join(parts), encoding="utf-8")
    finally:
        con.close()


def write_defaults(path: Path, step: str, enabled: bool, run_ids: list[str], report: RunReport) -> None:
    data: dict[str, Any] = yaml.safe_load(path.read_text()) if path.exists() else {}
    data[step] = {"enabled": enabled, "runs": run_ids, "ndcg10": round(report.ndcg10, 4), "recall100": round(report.recall100, 4)}
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
```

Note for the implementer: `ranx.compare` returns a `Report`; confirm the exact key shape of `report.comparisons` in the installed version (`uv run python -c "import ranx, inspect; print(inspect.signature(ranx.compare))"`) and adjust the `p` lookup — the test `test_gate_passes_identical_runs_only_on_significance` pins the behaviour, not the key shape. The per-slice tolerance uses the default's per-query values across all queries as the bootstrap population for the slice (small slices would otherwise have degenerate CIs); this is documented in the report output.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_report.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/report.py tests/search_eval/test_report.py
git commit -m "feat(search-eval): metrics, gate rules, side-by-side review page"
```

---

### Task 7: CLI entry point and CI regression check

**Files:**
- Create: `scripts/search_eval/__main__.py`
- Modify: `.github/workflows/ci.yml` (add a non-gating step), `README.md` (one paragraph under the existing search-correctness note)
- Test: `tests/search_eval/test_cli.py`

**Interfaces:**
- Produces: `python -m search_eval run --config NAME --index PATH [--depth 100]` → `data/search_eval/runs/NAME.trec` (refuses if `index_build_id` differs from `data/search_eval/build_id` when that file exists; writes it when absent); `pool --index PATH` → `pool.jsonl`; `judge --index PATH [--grader anthropic|fixed:N]` → `drafts.jsonl` and `review.jsonl`; `qrels [--adjudicated PATH]`; `report --default NAME --candidate NAME --index PATH [--n-tests K]` printing a markdown table and the gate verdict, writing `side_by_side.html`, exit code 1 when the gate fails.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_cli.py
import subprocess
import sys
from pathlib import Path

from ergon.index.build import build_index
from ergon.models import JobPosting

ROOT = Path(__file__).resolve().parents[2]


def test_run_then_report_round_trip(tmp_path):
    idx = tmp_path / "i.sqlite"
    build_index([JobPosting.create(source="greenhouse", source_job_id="1", company="A", title="Nurse")], idx, build_id="b1")
    data = tmp_path / "data"
    (data / "runs").mkdir(parents=True)
    (data / "queries.yaml").write_text("- {id: q1, text: nurse, slice: plain}\n")
    env = {"ERGON_SEARCH_EVAL_DATA": str(data), "PYTHONPATH": str(ROOT / "scripts")}
    r = subprocess.run([sys.executable, "-m", "search_eval", "run", "--config", "bm25", "--index", str(idx)],
                       capture_output=True, text=True, env={**env, "PATH": ""}, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert (data / "runs" / "bm25.trec").exists() and (data / "build_id").read_text().strip() == "b1"
    r2 = subprocess.run([sys.executable, "-m", "search_eval", "run", "--config", "bm25", "--index", str(idx)],
                        capture_output=True, text=True, env=env, cwd=ROOT)
    assert r2.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_cli.py -v`
Expected: FAIL (`No module named search_eval.__main__`)

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/search_eval/__main__.py
"""python -m search_eval {run,pool,judge,qrels,report}"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from search_eval import pool as pool_mod
from search_eval import qrels as qrels_mod
from search_eval import report as report_mod
from search_eval.judge import AnthropicGrader, draft
from search_eval.queries import load_queries
from search_eval.run import index_build_id, run_config, write_trec

DATA = Path(os.environ.get("ERGON_SEARCH_EVAL_DATA", "data/search_eval"))


class _Fixed:
    def __init__(self, n: int) -> None:
        self.n = n

    def grade(self, prompt: str) -> int:
        return self.n


def _pin(index: Path) -> None:
    bid = index_build_id(index)
    pin = DATA / "build_id"
    if pin.exists() and pin.read_text().strip() != bid:
        sys.exit(f"index build {bid} != pinned {pin.read_text().strip()}; runs are not comparable across builds")
    pin.write_text(bid + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="search_eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--config", required=True); r.add_argument("--index", type=Path, required=True); r.add_argument("--depth", type=int, default=100)
    po = sub.add_parser("pool"); po.add_argument("--index", type=Path, required=True)
    j = sub.add_parser("judge"); j.add_argument("--index", type=Path, required=True); j.add_argument("--grader", default="anthropic")
    q = sub.add_parser("qrels"); q.add_argument("--adjudicated", type=Path)
    rp = sub.add_parser("report"); rp.add_argument("--default", required=True); rp.add_argument("--candidate", required=True); rp.add_argument("--index", type=Path, required=True); rp.add_argument("--n-tests", type=int, default=1)
    a = p.parse_args(argv)
    queries = load_queries(DATA / "queries.yaml")
    runs_dir = DATA / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if a.cmd == "run":
        _pin(a.index)
        write_trec(run_config(a.config, a.index, queries, depth=a.depth), runs_dir / f"{a.config}.trec", a.config)
        return 0
    if a.cmd == "pool":
        from ranx import Run
        runs = {}
        for f in runs_dir.glob("*.trec"):
            rr = Run.from_file(str(f), kind="trec")
            runs[f.stem] = {qid: [(d, s) for d, s in docs.items()] for qid, docs in rr.run.items()}
        pairs = pool_mod.pool(runs, pool_mod.read_judged(DATA / "qrels.tsv"))
        n = pool_mod.write_pool(pairs, queries, a.index, DATA / "pool.jsonl")
        print(f"{n} pairs to judge -> {DATA / 'pool.jsonl'}")
        return 0
    if a.cmd == "judge":
        grader = _Fixed(int(a.grader.split(":")[1])) if a.grader.startswith("fixed:") else AnthropicGrader()
        n = draft(DATA / "pool.jsonl", a.index, grader, DATA / "drafts.jsonl")
        drafts = [json.loads(ln) for ln in (DATA / "drafts.jsonl").read_text().splitlines() if ln.strip()]
        pool_rows = {(x["qid"], x["job_id"]): x for x in map(json.loads, (DATA / "pool.jsonl").read_text().splitlines())}
        review = [{**pool_rows[(d["qid"], d["job_id"])], **d} for d in qrels_mod.select_for_review(drafts)]
        (DATA / "review.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in review) + "\n")
        print(f"{n} drafted; {len(review)} to review in scripts/search_eval/auditor.html")
        return 0
    if a.cmd == "qrels":
        n = qrels_mod.merge(DATA / "drafts.jsonl", a.adjudicated, DATA / "qrels.tsv")
        print(f"{n} judgments added -> {DATA / 'qrels.tsv'}")
        return 0
    if a.cmd == "report":
        g, b = report_mod.load_qrels(DATA / "qrels.tsv")
        d = report_mod.evaluate_run(runs_dir / f"{a.default}.trec", g, b, queries)
        c = report_mod.evaluate_run(runs_dir / f"{a.candidate}.trec", g, b, queries)
        res = report_mod.gate(c, d, g, n_tests=a.n_tests)
        print(f"| run | nDCG@10 | Recall@100 | unjudged |\n|---|---|---|---|")
        for x in (d, c):
            print(f"| {x.name} | {x.ndcg10:.4f} | {x.recall100:.4f} | {x.unjudged_rate:.0%} |")
        for s in d.by_slice:
            print(f"| {s} | {d.by_slice[s][0]:.3f} -> {c.by_slice.get(s, (0, 0))[0]:.3f} | {d.by_slice[s][1]:.3f} -> {c.by_slice.get(s, (0, 0))[1]:.3f} | |")
        report_mod.side_by_side(runs_dir / f"{a.default}.trec", runs_dir / f"{a.candidate}.trec", queries, DATA / "qrels.tsv", a.index, DATA / "side_by_side.html")
        print("GATE:", "PASS" if res.passed else "FAIL", *("\n  - " + r for r in res.reasons))
        return 0 if res.passed else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

CI: add to `.github/workflows/ci.yml` after the test step a step named `search-eval regression (non-gating)` with `continue-on-error: true` that runs `uv run python -m search_eval report --default bm25 --candidate bm25 --index dist/index-slim.sqlite --n-tests 1 || true` only `if: hashFiles('data/search_eval/qrels.tsv') != ''` — it exercises the harness on committed data; it cannot gate because the index is not present in CI (the step downloads `index-slim.sqlite.gz` from the release the same way `build-index.yml` does; if the download fails the step is skipped with a notice). README: one paragraph "Search quality is measured on a judged query set (`data/search_eval/`); see the spec in `docs/superpowers/specs/`."

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval/__main__.py tests/search_eval/test_cli.py .github/workflows/ci.yml README.md
git commit -m "feat(search-eval): CLI entry point and non-gating CI regression step"
```

---

### Task 8: Author the query set, produce the baseline, first judging session

This task is mechanics plus a human session; it produces the committed baseline every later step is gated on.

**Files:**
- Modify: `data/search_eval/queries.yaml` (60 queries), `data/search_eval/qrels.tsv`, `data/search_eval/runs/bm25.trec`, `data/search_eval/build_id`, `data/search_eval/defaults.yaml`

- [ ] **Step 1: Write the 60 queries** — 12 per slice. Constraint queries carry `expect`. Examples to include verbatim: `software engineer position in NYC for >130k new grad` (`expect: {city: New York, salary_min: 130000, max_years: 1}`), `ML engineer`, `SDE`, `data scientist NLP`, `entry level analyst`, `senior backend remote`, `C++`, `Rust`, `Kubernetes`, `CPA`, `nurse practitioner Chicago`, `product manager fintech London`. Validate: `uv run python -c "import sys; sys.path.insert(0,'scripts'); from search_eval.queries import load_queries; from pathlib import Path; print(len(load_queries(Path('data/search_eval/queries.yaml'))))"` → `60`.

- [ ] **Step 2: Baseline run** — download the current slim index (`gh release download index-latest -p index-slim.sqlite.gz -D dist && gunzip -f dist/index-slim.sqlite.gz`; the slim tier has no `snippet`, so use the full `index.sqlite.gz` instead — 2.7 GB open; delete it after). Run `uv run python -m search_eval run --config bm25 --index dist/index.sqlite`. Commit `runs/bm25.trec` and `build_id`.

- [ ] **Step 3: Pool and draft** — `uv run python -m search_eval pool --index dist/index.sqlite` (expect ~1,200 pairs), then `ANTHROPIC_API_KEY=… uv run python -m search_eval judge --index dist/index.sqlite`.

- [ ] **Step 4: Adjudicate** — open `scripts/search_eval/auditor.html`, load `data/search_eval/review.jsonl`, grade, download `adjudicated.jsonl`, then `uv run python -m search_eval qrels --adjudicated ~/Downloads/adjudicated.jsonl`.

- [ ] **Step 5: Baseline report and defaults** — `uv run python -m search_eval report --default bm25 --candidate bm25 --index dist/index.sqlite` prints the baseline numbers (the gate fails on identical runs by design). Write `defaults.yaml` with `bm25: {enabled: true, runs: [bm25], …}` via `report_mod.write_defaults` from a one-off `python -c`. Commit everything: `git commit -m "data(search-eval): 60-query judged set, BM25 baseline run and qrels"`. Delete `dist/index.sqlite`.

---

## Phase B — Query understanding

### Task 9: Salary extractor

**Files:**
- Create: `src/ergon/query/__init__.py` (empty), `src/ergon/query/salary.py`
- Test: `tests/query/__init__.py` (empty), `tests/query/test_salary.py`

**Interfaces:**
- Produces: `Span(start: int, end: int, text: str)`; `extract_salary(text: str) -> tuple[SalaryParse | None, list[Span]]` where `SalaryParse(min: float | None, max: float | None, currency: str | None, interval: str | None)`; amounts normalized to annual when `interval` is stated (`hour`×2080, `day`×260, `week`×52, `month`×12), `interval="year"` after normalization, `None` when unstated (amount left as-is).

- [ ] **Step 1: Write the failing test**

```python
# tests/query/test_salary.py
import pytest

from ergon.query.salary import SalaryParse, extract_salary


@pytest.mark.parametrize("text, exp", [
    (">130k", SalaryParse(130000, None, None, None)),
    ("130k+", SalaryParse(130000, None, None, None)),
    ("$130,000", SalaryParse(130000, 130000, "USD", None)),
    ("130-160k", SalaryParse(130000, 160000, None, None)),
    ("over 130k", SalaryParse(130000, None, None, None)),
    ("at least 100k", SalaryParse(100000, None, None, None)),
    ("€80k", SalaryParse(80000, 80000, "EUR", None)),
    ("£65k", SalaryParse(65000, 65000, "GBP", None)),
    ("$45/hr", SalaryParse(93600, 93600, "USD", "year")),
    ("8k per month", SalaryParse(96000, 96000, None, "year")),
    ("under 90k", SalaryParse(None, 90000, None, None)),
])
def test_extracts(text, exp):
    got, spans = extract_salary(f"software engineer {text} remote")
    assert got == exp
    assert spans and all(text.split()[0] in s.text or s.text in f"{text}" for s in spans)


@pytest.mark.parametrize("text", ["3 years", "C++ 17", "Java 8", "2 openings", "$95,000 M", "top 10 company"])
def test_never_a_salary(text):
    got, spans = extract_salary(text)
    assert got is None and spans == []


def test_k_suffix_followed_by_letter_is_not_a_multiplier():
    got, _ = extract_salary("$95,000 M")
    assert got is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/query/test_salary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ergon.query'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ergon/query/salary.py
"""Salary constraints in free text: amounts, ranges, currency, pay period."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CUR = {"$": "USD", "€": "EUR", "£": "GBP"}
_PERIOD = {"hour": 2080, "hr": 2080, "h": 2080, "day": 260, "week": 52, "wk": 52, "month": 12, "mo": 12, "year": 1, "yr": 1, "annum": 1}
_AMT = r"(?P<cur>[$€£])?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<k>[kK])?(?![A-Za-z])"
_RANGE = re.compile(rf"(?P<lo>{_AMT})\s*(?:-|–|to)\s*(?P<hi>{_AMT.replace('?P<cur>', '?P<cur2>').replace('?P<num>', '?P<num2>').replace('?P<k>', '?P<k2>')})")
_SINGLE = re.compile(rf"(?P<op>>=?|<=?|over|above|at least|min(?:imum)?|under|below|up to|max(?:imum)?)?\s*{_AMT}\s*(?P<plus>\+)?(?:\s*(?:/|per|a|an)\s*(?P<per>hour|hr|h|day|week|wk|month|mo|year|yr|annum))?", re.IGNORECASE)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class SalaryParse:
    min: float | None
    max: float | None
    currency: str | None
    interval: str | None


def _amount(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _money_like(cur: str | None, k: str | None, num: str, op: str | None, plus: str | None, per: str | None) -> bool:
    return bool(cur or k or per or ("," in num and len(num.replace(",", "")) >= 5))


def _annual(v: float | None, per: str | None) -> float | None:
    return None if v is None else v * _PERIOD[per.lower()] if per else v


def extract_salary(text: str) -> tuple[SalaryParse | None, list[Span]]:
    m = _RANGE.search(text)
    if m and _money_like(m.group("cur") or m.group("cur2"), m.group("k") or m.group("k2"), m.group("num"), None, None, None):
        k = m.group("k2") or m.group("k")
        lo, hi = _amount(m.group("num"), k), _amount(m.group("num2"), m.group("k2") or k)
        cur = _CUR.get(m.group("cur") or m.group("cur2") or "")
        return SalaryParse(lo, hi, cur, None), [Span(m.start(), m.end(), m.group(0))]
    for m in _SINGLE.finditer(text):
        op, cur, num, k, plus, per = (m.group(g) for g in ("op", "cur", "num", "k", "plus", "per"))
        if not _money_like(cur, k, num, op, plus, per):
            continue
        v = _annual(_amount(num, k), per)
        interval = "year" if per else None
        o = (op or "").lower()
        if o in (">", ">=", "over", "above", "at least", "min", "minimum") or plus:
            sp = SalaryParse(v, None, _CUR.get(cur or ""), interval)
        elif o in ("<", "<=", "under", "below", "up to", "max", "maximum"):
            sp = SalaryParse(None, v, _CUR.get(cur or ""), interval)
        else:
            sp = SalaryParse(v, v, _CUR.get(cur or ""), interval)
        return sp, [Span(m.start(), m.end(), m.group(0).strip())]
    return None, []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/query/test_salary.py -v`
Expected: PASS. If a parametrized case fails, adjust the regex — the cases are the contract; do not delete cases.

- [ ] **Step 5: Commit**

```bash
git add src/ergon/query tests/query
git commit -m "feat(query): salary extractor with period normalization and the documented traps"
```

---

### Task 10: Experience / level extractor

**Files:**
- Create: `src/ergon/query/experience.py`
- Test: `tests/query/test_experience.py`

**Interfaces:**
- Consumes: `ergon.extract.level.infer_level(title: str) -> JobLevel`, `ergon.models.JobLevel`.
- Produces: `ExperienceParse(min_years: int | None, max_years: int | None, level: JobLevel | None)`; `extract_experience(text: str) -> tuple[ExperienceParse | None, list[Span]]`. Rule: an explicit years span wins over a level word; phrase table per spec §4.1.

- [ ] **Step 1: Write the failing test**

```python
# tests/query/test_experience.py
import pytest

from ergon.models import JobLevel
from ergon.query.experience import ExperienceParse, extract_experience


@pytest.mark.parametrize("text, exp", [
    ("new grad software engineer", ExperienceParse(None, 1, JobLevel.ENTRY)),
    ("recent graduate analyst", ExperienceParse(None, 1, JobLevel.ENTRY)),
    ("early career data scientist", ExperienceParse(None, 1, JobLevel.ENTRY)),
    ("entry level analyst", ExperienceParse(None, 2, JobLevel.ENTRY)),
    ("junior developer", ExperienceParse(None, 2, JobLevel.JUNIOR)),
    ("0-2 years experience python", ExperienceParse(0, 2, None)),
    ("3+ years java", ExperienceParse(3, None, None)),
    ("5-8 yrs backend", ExperienceParse(5, 8, None)),
    ("internship marketing", ExperienceParse(None, None, JobLevel.INTERN)),
    ("senior backend remote", ExperienceParse(None, None, JobLevel.SENIOR)),
    ("staff engineer", ExperienceParse(None, None, JobLevel.STAFF)),
    ("engineering manager", ExperienceParse(None, None, JobLevel.MANAGER)),
])
def test_extracts(text, exp):
    got, spans = extract_experience(text)
    assert got == exp and spans


def test_years_win_over_the_level_word():
    got, _ = extract_experience("entry level engineer 4+ years")
    assert got == ExperienceParse(4, None, JobLevel.ENTRY)


@pytest.mark.parametrize("text", ["C++ 17", "Java 8", "2 openings", "python developer", "top 10"])
def test_no_signal(text):
    got, spans = extract_experience(text)
    assert got is None and spans == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/query/test_experience.py -v`
Expected: FAIL (`No module named 'ergon.query.experience'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/ergon/query/experience.py
"""Experience and seniority in free text; explicit years beat level words."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..extract.level import infer_level
from ..models import JobLevel
from .salary import Span

_NEW_GRAD = re.compile(r"\b(?:new[- ]grad(?:uate)?s?|recent[- ]graduates?|graduate program(?:me)?|campus(?: hire)?|university hire|early[- ]career)\b", re.I)
_ENTRY = re.compile(r"\b(?:entry[- ]level|junior|jr\.?)\b", re.I)
_INTERN = re.compile(r"\b(?:interns?|internships?)\b", re.I)
_YEARS = re.compile(r"\b(?P<lo>\d{1,2})\s*(?:-|–|to)\s*(?P<hi>\d{1,2})\s*(?:\+\s*)?(?:years?|yrs?)\b|\b(?P<n>\d{1,2})\s*(?P<plus>\+)?\s*(?:years?|yrs?)\b", re.I)
_LEVEL_WORDS = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|executive|vp|head of)\b", re.I)


@dataclass(frozen=True)
class ExperienceParse:
    min_years: int | None
    max_years: int | None
    level: JobLevel | None


def extract_experience(text: str) -> tuple[ExperienceParse | None, list[Span]]:
    spans: list[Span] = []
    lo = hi = None
    level: JobLevel | None = None
    m = _YEARS.search(text)
    if m:
        if m.group("lo"):
            lo, hi = int(m.group("lo")), int(m.group("hi"))
        else:
            lo = int(m.group("n"))
            hi = None if m.group("plus") else int(m.group("n"))
        spans.append(Span(m.start(), m.end(), m.group(0)))
    for rx, lvl, cap in ((_NEW_GRAD, JobLevel.ENTRY, 1), (_ENTRY, None, 2), (_INTERN, JobLevel.INTERN, None)):
        w = rx.search(text)
        if w:
            level = lvl or (JobLevel.JUNIOR if w.group(0).lower().startswith(("junior", "jr")) else JobLevel.ENTRY)
            if cap is not None and not m:
                hi = cap
            spans.append(Span(w.start(), w.end(), w.group(0)))
            break
    if level is None:
        w = _LEVEL_WORDS.search(text)
        if w:
            inferred = infer_level(text)
            if inferred is not JobLevel.UNKNOWN:
                level = inferred
                spans.append(Span(w.start(), w.end(), w.group(0)))
    if lo is None and hi is None and level is None:
        return None, []
    return ExperienceParse(lo, hi, level), spans
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/query/test_experience.py -v`
Expected: PASS. `infer_level` must return SENIOR/STAFF/MANAGER for the titles in the table; if it returns UNKNOWN for one, extend the test to document it and map that word explicitly in `_LEVEL_WORDS` handling rather than weakening the case.

- [ ] **Step 5: Commit**

```bash
git add src/ergon/query/experience.py tests/query/test_experience.py
git commit -m "feat(query): experience and level extractor; years beat level words"
```

---

### Task 11: Location gazetteer and extractor

**Files:**
- Create: `scripts/build_query_places.py`, `data/query_places.yaml`, `NOTICE`, `src/ergon/query/location.py`
- Test: `tests/query/test_location.py`, `tests/test_build_query_places.py`

**Interfaces:**
- Consumes: `ergon.extract.geo.city_match_terms(city) -> list[str]`, `country_match_term(country) -> str`.
- Produces: gazetteer YAML `{cities: [{name, country, admin, aliases: [...]}], countries: {code: name}, states: {code: name}}`; `build_places(cities_txt: Path, index: Path | None, out: Path, *, min_pop: int = 100000) -> int`; `LocationParse(city: str | None, country: str | None, remote: bool | None)`; `extract_location(text: str) -> tuple[LocationParse | None, list[Span]]` — longest alias match first, case-insensitive, whole-word; `remote`/`hybrid`/`on-site` words set `remote`.

GeoNames source: `https://download.geonames.org/export/dump/cities15000.zip` (tab-separated; columns: geonameid, name, asciiname, alternatenames, lat, lon, feature class, feature code, country code, cc2, admin1, admin2, admin3, admin4, population, …). Keep rows with `population >= min_pop` **plus** every `(city, country)` present in the index's `jobs` table when `index` is given. Curated additions in the generator (not from GeoNames): `NYC → New York`, `SF → San Francisco`, `Bay Area → San Francisco`, `LA → Los Angeles`, `DC → Washington`, `Philly → Philadelphia`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_query_places.py
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_query_places import build_places  # noqa: E402

_ROW = "{gid}\t{name}\t{name}\t{alts}\t0\t0\tP\tPPL\t{cc}\t\t{adm}\t\t\t\t{pop}\t\t\t\tUTC\t2026-01-01"


def test_population_floor_and_curated_aliases(tmp_path):
    src = tmp_path / "cities15000.txt"
    src.write_text("\n".join([
        _ROW.format(gid=1, name="New York City", alts="NYC,New York", cc="US", adm="NY", pop=8000000),
        _ROW.format(gid=2, name="Tinytown", alts="", cc="US", adm="TX", pop=20000),
    ]) + "\n")
    out = tmp_path / "places.yaml"
    n = build_places(src, None, out)
    data = yaml.safe_load(out.read_text())
    names = {c["name"] for c in data["cities"]}
    assert "New York City" in names and "Tinytown" not in names and n == 1
    nyc = next(c for c in data["cities"] if c["name"] == "New York City")
    assert {"NYC", "New York"} <= set(nyc["aliases"])
```

```python
# tests/query/test_location.py
import pytest

from ergon.query.location import LocationParse, extract_location


@pytest.mark.parametrize("text, exp", [
    ("software engineer in NYC", LocationParse("New York", "US", None)),
    ("sf backend engineer", LocationParse("San Francisco", "US", None)),
    ("data analyst Chicago IL", LocationParse("Chicago", "US", None)),
    ("nurse London UK", LocationParse("London", "GB", None)),
    ("remote python developer", LocationParse(None, None, True)),
    ("hybrid role Berlin", LocationParse("Berlin", "DE", False)),
    ("engineer based in Austin", LocationParse("Austin", "US", None)),
])
def test_extracts(text, exp):
    got, spans = extract_location(text)
    assert got == exp and spans


@pytest.mark.parametrize("text", ["python developer", "reading specialist", "phoenix framework elixir"])
def test_no_false_city(text):
    got, _ = extract_location(text)
    assert got is None or got.city is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_build_query_places.py tests/query/test_location.py -v`
Expected: FAIL (modules missing)

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/build_query_places.py
"""Generate data/query_places.yaml from GeoNames cities15000.txt (CC-BY 4.0) plus curated aliases."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml

CURATED = {"New York City": ["NYC", "New York"], "San Francisco": ["SF", "Bay Area", "SF Bay Area"],
           "Los Angeles": ["LA"], "Washington": ["DC", "Washington DC", "Washington, D.C."], "Philadelphia": ["Philly"]}
AMBIGUOUS = {"Reading", "Phoenix", "Mobile", "Nice", "Bath", "Orange", "Independence", "Normal", "Surprise"}


def build_places(cities_txt: Path, index: Path | None, out: Path, *, min_pop: int = 100000) -> int:
    keep: set[tuple[str, str]] = set()
    if index is not None:
        con = sqlite3.connect(index)
        keep = {(c, k) for c, k in con.execute("SELECT DISTINCT city, country FROM jobs WHERE city IS NOT NULL")}
        con.close()
    cities = []
    for line in cities_txt.read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) < 15:
            continue
        name, alts, cc, adm, pop = f[1], f[3], f[8], f[10], int(f[14] or 0)
        if pop < min_pop and (name, cc) not in keep:
            continue
        aliases = sorted({a for a in alts.split(",") if a and a.isascii() and len(a) >= 2 and a != name} | set(CURATED.get(name, [])))
        cities.append({"name": name, "country": cc, "admin": adm, "aliases": aliases, "ambiguous": name in AMBIGUOUS})
    out.write_text(yaml.safe_dump({"cities": cities}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return len(cities)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cities", type=Path, required=True)
    p.add_argument("--index", type=Path)
    p.add_argument("--out", type=Path, default=Path("data/query_places.yaml"))
    a = p.parse_args()
    sys.exit(0 if build_places(a.cities, a.index, a.out) else 1)
```

```python
# src/ergon/query/location.py
"""Places in free text, via the committed gazetteer; remote/hybrid/on-site words."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

import yaml

from .salary import Span

_REMOTE = re.compile(r"\b(remote(?:-first)?|work from home|wfh)\b", re.I)
_ONSITE = re.compile(r"\b(hybrid|on-?site|in-?office)\b", re.I)
_PREP = re.compile(r"\b(?:in|near|based in|located in|around)\s+$", re.I)
_STATES = {"NY": "New York", "CA": "California", "IL": "Illinois", "TX": "Texas", "MA": "Massachusetts", "WA": "Washington"}
_COUNTRIES = {"UK": "GB", "USA": "US", "US": "US", "GB": "GB", "DE": "DE", "FR": "FR", "CA": "CA", "IN": "IN"}


@dataclass(frozen=True)
class LocationParse:
    city: str | None
    country: str | None
    remote: bool | None


@lru_cache(maxsize=1)
def _gazetteer() -> list[tuple[str, str, str, bool]]:
    data = yaml.safe_load(files("ergon").joinpath("../../data/query_places.yaml").read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str, bool]] = []
    for c in data["cities"]:
        canon = "New York" if c["name"] == "New York City" else c["name"]
        for alias in [c["name"], *c["aliases"]]:
            rows.append((alias.lower(), canon, c["country"], bool(c.get("ambiguous"))))
    rows.sort(key=lambda r: -len(r[0]))
    return rows


def extract_location(text: str) -> tuple[LocationParse | None, list[Span]]:
    spans: list[Span] = []
    remote: bool | None = None
    m = _REMOTE.search(text)
    if m:
        remote = True
        spans.append(Span(m.start(), m.end(), m.group(0)))
    m = _ONSITE.search(text)
    if m:
        remote = False
        spans.append(Span(m.start(), m.end(), m.group(0)))
    low = text.lower()
    city = country = None
    for alias, canon, cc, ambiguous in _gazetteer():
        for mm in re.finditer(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", low):
            preceded = bool(_PREP.search(low[: mm.start()]))
            if ambiguous and not preceded and len(alias) > 3:
                continue
            if len(alias) <= 3 and alias.isalpha() and text[mm.start(): mm.end()] != alias.upper() and not preceded:
                continue
            city, country = canon, cc
            spans.append(Span(mm.start(), mm.end(), text[mm.start(): mm.end()]))
            break
        if city:
            break
    for code, cc in _COUNTRIES.items():
        mm = re.search(rf"\b{code}\b", text)
        if mm:
            country = country or cc
            spans.append(Span(mm.start(), mm.end(), mm.group(0)))
            break
    if city is None and country is None and remote is None:
        return None, []
    return LocationParse(city, country, remote), spans
```

Generate the data: download `cities15000.zip`, unzip, run `uv run python scripts/build_query_places.py --cities cities15000.txt` (with `--index dist/index.sqlite` when the index is on disk), commit `data/query_places.yaml`. Create `NOTICE` with two paragraphs: GeoNames (CC-BY 4.0, https://www.geonames.org) and O*NET (CC-BY 4.0, U.S. Department of Labor, Employment and Training Administration; the O*NET line is used by Task 14). Fix the gazetteer path: ship `data/query_places.yaml` inside the package as `src/ergon/query/places.yaml` (copy in the generator via `--out src/ergon/query/places.yaml`) and load with `files("ergon.query").joinpath("places.yaml")`; add `"ergon.query" = ["*.yaml"]` to package data in `pyproject.toml`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_build_query_places.py tests/query/test_location.py -v`
Expected: PASS. The `sf` case requires the alias to match only in upper case for ≤3-letter aliases unless preceded by a preposition; `"sf backend engineer"` is lower case, so change that case to `"SF backend engineer"` — a bare lower-case `sf` is deliberately not a city.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_query_places.py src/ergon/query/location.py src/ergon/query/places.yaml NOTICE pyproject.toml tests/test_build_query_places.py tests/query/test_location.py
git commit -m "feat(query): location extractor over a GeoNames-derived gazetteer (CC-BY 4.0)"
```

---

### Task 12: Employment type, assembly, explain, miss log

**Files:**
- Create: `src/ergon/query/employment.py`, `src/ergon/query/understand.py`
- Modify: `src/ergon/models.py` (add `understand: bool | None = None`, `aliases: bool | None = None`, `rerank: bool | None = None`, `dense: bool | None = None` to `SearchQuery`)
- Test: `tests/query/test_understand.py`

**Interfaces:**
- Produces: `Parse(fields: dict[str, Any], spans: dict[str, str], role: str | None)`; `understand(text: str, base: SearchQuery | None = None) -> tuple[SearchQuery, Parse]` — fills only fields that are `None` on `base`; `keywords` becomes the role phrase (or `None`); never raises; `ERGON_QUERY_LOG` appends one JSON line per call when set.

- [ ] **Step 1: Write the failing test**

```python
# tests/query/test_understand.py
import json

from hypothesis import given, strategies as st

from ergon.models import JobLevel, SearchQuery
from ergon.query.understand import understand


def test_the_example_query():
    q, p = understand("software engineer position in NYC for >130k new grad")
    assert q.keywords == "software engineer position"
    assert q.city == "New York" and q.country == "US"
    assert q.salary_min == 130000 and q.max_years == 1 and q.level == JobLevel.ENTRY
    assert set(p.spans) >= {"salary_min", "city", "max_years"}


def test_explicit_values_win():
    base = SearchQuery(keywords="ignored", city="Boston", salary_min=1)
    q, _ = understand("engineer in NYC >130k", base)
    assert q.city == "Boston" and q.salary_min == 1


def test_pure_constraints_leave_keywords_none():
    q, _ = understand("remote >150k senior")
    assert q.keywords is None and q.remote is True and q.salary_min == 150000


def test_employment_type():
    q, _ = understand("part-time nurse Chicago")
    assert q.employment_type.value == "part_time" and q.keywords == "nurse"


@given(st.text(max_size=200))
def test_never_raises_and_never_empties(text):
    q, p = understand(text)
    assert isinstance(q, SearchQuery)
    assert (q.keywords or "").strip() == (q.keywords or "")


def test_miss_log(tmp_path, monkeypatch):
    log = tmp_path / "q.jsonl"
    monkeypatch.setenv("ERGON_QUERY_LOG", str(log))
    understand("some query")
    row = json.loads(log.read_text().splitlines()[0])
    assert row["text"] == "some query" and "parse" in row
```

Add `"hypothesis>=6.100"` to the `dev` dependencies.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/query/test_understand.py -v`
Expected: FAIL (`No module named 'ergon.query.understand'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/ergon/query/employment.py
"""Employment type words."""

from __future__ import annotations

import re

from ..models import EmploymentType
from .salary import Span

_RX = {
    EmploymentType.FULL_TIME: re.compile(r"\bfull[- ]?time\b", re.I),
    EmploymentType.PART_TIME: re.compile(r"\bpart[- ]?time\b", re.I),
    EmploymentType.CONTRACT: re.compile(r"\b(?:contract(?:or)?|freelance|c2c)\b", re.I),
    EmploymentType.INTERNSHIP: re.compile(r"\b(?:interns?|internships?)\b", re.I),
}


def extract_employment(text: str) -> tuple[EmploymentType | None, list[Span]]:
    for et, rx in _RX.items():
        m = rx.search(text)
        if m:
            return et, [Span(m.start(), m.end(), m.group(0))]
    return None, []
```

```python
# src/ergon/query/understand.py
"""understand(text) -> (SearchQuery, Parse): free text into the filters SearchQuery already has."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import JobLevel, SearchQuery
from .employment import extract_employment
from .experience import extract_experience
from .location import extract_location
from .salary import Span, extract_salary


@dataclass
class Parse:
    fields: dict[str, Any] = field(default_factory=dict)
    spans: dict[str, str] = field(default_factory=dict)
    role: str | None = None


def _cut(text: str, spans: list[Span]) -> str:
    out = text
    for s in sorted(spans, key=lambda s: -s.start):
        out = out[: s.start] + " " + out[s.end :]
    out = re.sub(r"\b(?:in|for|at|near|based in|position|positions|jobs?|roles?)\b", " ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" ,-–") or ""


def understand(text: str, base: SearchQuery | None = None) -> tuple[SearchQuery, Parse]:
    base = base or SearchQuery()
    parse = Parse()
    consumed: list[Span] = []
    fields: dict[str, Any] = {}

    def take(fn):  # noqa: ANN001, ANN202 - one extractor; a failure leaves its text in the role phrase
        try:
            return fn(text)
        except Exception:  # noqa: BLE001
            return None, []

    sal, sp = take(extract_salary)
    if sal:
        fields.update({"salary_min": sal.min, "salary_max": sal.max, "salary_currency": sal.currency})
        consumed += sp
        for k in ("salary_min", "salary_max"):
            if fields.get(k) is not None:
                parse.spans[k] = sp[0].text
    exp, sp = take(extract_experience)
    if exp:
        fields.update({"min_years": exp.min_years, "max_years": exp.max_years, "level": exp.level})
        consumed += sp
        for k, v in (("min_years", exp.min_years), ("max_years", exp.max_years), ("level", exp.level)):
            if v is not None:
                parse.spans[k] = " ".join(s.text for s in sp)
    loc, sp = take(extract_location)
    if loc:
        fields.update({"city": loc.city, "country": loc.country, "remote": loc.remote})
        consumed += sp
        for k, v in (("city", loc.city), ("country", loc.country), ("remote", loc.remote)):
            if v is not None:
                parse.spans[k] = " ".join(s.text for s in sp)
    et, sp = take(extract_employment)
    if et:
        fields["employment_type"] = et
        consumed += sp
        parse.spans["employment_type"] = sp[0].text
        if et.value == "internship" and fields.get("level") is None:
            fields["level"] = JobLevel.INTERN
    role = _cut(text, consumed) or None
    parse.role = role
    update: dict[str, Any] = {}
    for k, v in fields.items():
        if v is not None and getattr(base, k, None) is None:
            update[k] = v
            parse.fields[k] = v
    if base.keywords is None or base.keywords == text:
        update["keywords"] = role
    q = base.model_copy(update=update)
    log = os.environ.get("ERGON_QUERY_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"text": text, "parse": {"fields": {k: str(v) for k, v in parse.fields.items()}, "role": role}}) + "\n")
    return q, parse
```

In `models.py`, add to `SearchQuery` (after `semantic`): `understand: bool | None = None`, `aliases: bool | None = None`, `rerank: bool | None = None`, `dense: bool | None = None` — each with a one-line comment "None = package default (data/search_eval/defaults.yaml)".

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/query -v`
Expected: PASS. The example query's `keywords` must be exactly `"software engineer position"`; if `_cut` leaves a stray word, extend the stop-word list in `_cut`, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/ergon/query src/ergon/models.py tests/query pyproject.toml uv.lock
git commit -m "feat(query): understand() assembles the extractors into SearchQuery; explain and miss log"
```

---

### Task 13: Wire `understand()` into the surfaces; eval run

**Files:**
- Modify: `src/ergon/engine.py:run_search` (before `try_index_ranked`), `src/ergon/mcp_server.py` (the `search_jobs` tool before `try_index_ranked(query)` at ~line 260; put `parse` into the result `meta`), `src/ergon/cli.py` (`--explain` flag printing `Parse`; `--no-understand`), `scripts/search_eval/configs.py` (add `understand` config), `src/ergon/query/defaults.py` (new: `default_for(step: str) -> bool`, reading `defaults.yaml` shipped as `src/ergon/query/defaults.yaml`, `False` when absent)
- Test: `tests/query/test_wiring.py`

**Interfaces:**
- Produces: `apply_understanding(query: SearchQuery) -> tuple[SearchQuery, Parse | None]` in `understand.py`: returns the query unchanged when `query.understand is False`, or when `query.understand is None and not default_for("understand")`, or when `query.keywords` is empty; else `understand(query.keywords, query)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/query/test_wiring.py
from ergon.models import SearchQuery
from ergon.query.understand import apply_understanding


def test_off_by_default_until_the_gate_says_otherwise(monkeypatch):
    import ergon.query.defaults as d
    monkeypatch.setattr(d, "default_for", lambda step: False)
    q, p = apply_understanding(SearchQuery(keywords="engineer NYC"))
    assert q.city is None and p is None


def test_explicit_true_parses(monkeypatch):
    q, p = apply_understanding(SearchQuery(keywords="engineer NYC", understand=True))
    assert q.city == "New York" and p is not None and q.keywords == "engineer"


def test_explicit_false_never_parses():
    q, p = apply_understanding(SearchQuery(keywords="engineer NYC", understand=False))
    assert q.city is None and p is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/query/test_wiring.py -v`
Expected: FAIL (`cannot import name 'apply_understanding'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/ergon/query/defaults.py
"""Package defaults for the gated steps, written by the eval harness (never by hand)."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

import yaml


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return yaml.safe_load(files("ergon.query").joinpath("defaults.yaml").read_text()) or {}
    except (FileNotFoundError, OSError):
        return {}


def default_for(step: str) -> bool:
    return bool((_load().get(step) or {}).get("enabled", False))
```

Append to `understand.py`:

```python
def apply_understanding(query: SearchQuery) -> tuple[SearchQuery, Parse | None]:
    from .defaults import default_for

    on = query.understand if query.understand is not None else default_for("understand")
    if not on or not query.keywords:
        return query, None
    return understand(query.keywords, query)
```

`engine.run_search`: `query, parse = apply_understanding(query)` as the first statement after `load_plugins()`; attach `parse` to the returned `SearchResult` as a new optional field `parse: dict | None` (add to the `SearchResult` model as `parse: dict[str, Any] | None = None`). MCP `search_jobs`: same call before `try_index_ranked(query)`; include `{"parse": {"fields": ..., "role": ...}}` in the tool's returned `meta`. CLI `search`: add `--explain` (prints `parse.fields` and `parse.spans` as a table to stderr via `err_console`) and `--no-understand` (sets `understand=False`). The harness `data/search_eval/defaults.yaml` is copied into `src/ergon/query/defaults.yaml` by `report_mod.write_defaults` (write both paths). `configs.py`: `CONFIGS["understand"] = Config("understand", lambda t: SearchQuery(keywords=t, understand=True))`, and the approach-B oracle: `CONFIGS["oracle"]` is built per query from its `expect` — because `Config.build` only sees the text, give `run_config` an optional `expect_for: Callable[[str], dict]` hook set from the loaded queries, and define `_oracle(t, expect) -> SearchQuery(keywords=understand(t)[1].role, **expect)`. The `report` command, when `--candidate understand`, also prints per slice: the share of `expect` fields the parser filled (from the `Parse` recorded during the run into `runs/understand.parse.jsonl`) and the `oracle` run's Recall@100 — the numbers the approach-B trigger in spec §7 reads.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/query tests/search_eval tests/test_cli*.py tests/test_mcp*.py -q`
Expected: PASS

- [ ] **Step 5: Eval run and commit**

`uv run python -m search_eval run --config understand --index dist/index.sqlite && uv run python -m search_eval pool --index dist/index.sqlite && uv run python -m search_eval judge --index dist/index.sqlite` → adjudicate → `qrels` → `uv run python -m search_eval report --default bm25 --candidate understand --index dist/index.sqlite`. If the gate passes, `write_defaults(..., "understand", True, ["bm25", "understand"], report)`. Commit code and data together:

```bash
git add src/ergon tests scripts/search_eval/configs.py data/search_eval
git commit -m "feat(query): apply understand() on every surface; eval run 'understand' and its gate report"
```

---

## Phase C — Title aliases

### Task 14: Alias table generator

**Files:**
- Create: `scripts/build_title_aliases.py`, `data/title_aliases.overrides.yaml`, `src/ergon/query/title_aliases.yaml` (generated), `tests/fixtures/onet_job_titles_slice.csv`
- Modify: `NOTICE` (O*NET paragraph if not yet present), `pyproject.toml` package data
- Test: `tests/test_build_title_aliases.py`

**Interfaces:**
- Produces: `build_aliases(onet_csv: Path, overrides: Path, out: Path) -> int`; output YAML `{"15-1252.00": {label, aliases: [...], acronyms: [...], related: [{code, reason}]}, ...}`; overrides YAML `{"15-1252.00": {acronyms: [SWE, SDE], related: [{code: "15-2051.00", reason: "..."}], add: [...], drop: [...]}}`. Parentheticals in a title are split into separate aliases.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_title_aliases.py
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_title_aliases import build_aliases  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "onet_job_titles_slice.csv"


def test_generates_aliases_acronyms_and_related(tmp_path):
    ov = tmp_path / "ov.yaml"
    ov.write_text(yaml.safe_dump({"15-1252.00": {"acronyms": ["SWE", "SDE"], "related": [{"code": "15-2051.00", "reason": "MLE hired as software"}], "drop": ["Beta Tester"]}}))
    out = tmp_path / "aliases.yaml"
    n = build_aliases(FIX, ov, out)
    data = yaml.safe_load(out.read_text())
    sd = data["15-1252.00"]
    assert n >= 2 and sd["label"] == "Software Developers"
    assert "Software Engineer" in sd["aliases"] and "Beta Tester" not in sd["aliases"]
    assert {"UI Designer", "User Interface Designer"} <= set(sd["aliases"]), "parentheticals are split"
    assert sd["acronyms"] == ["SDE", "SWE"] and sd["related"][0]["code"] == "15-2051.00"
```

Fixture `tests/fixtures/onet_job_titles_slice.csv` (comma-separated, header as in O*NET):

```
O*NET-SOC Code,Title,Job Title,Short Title,Source(s)
15-1252.00,Software Developers,Software Engineer,,10
15-1252.00,Software Developers,Beta Tester,,10
15-1252.00,Software Developers,UI Designer (User Interface Designer),,10
15-2051.00,Data Scientists,Machine Learning Engineer,,10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build_title_aliases.py -v`
Expected: FAIL (`No module named 'build_title_aliases'`)

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/build_title_aliases.py
"""Generate the title-alias table from O*NET job_titles.csv (CC-BY 4.0) plus curated overrides."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import yaml

ONET_URL = "https://www.onetcenter.org/dl_files/database/db_30_3_csv/job_titles.csv"
_PAREN = re.compile(r"^(?P<a>[^()]+?)\s*\((?P<b>[^()]+)\)\s*$")


def _split(title: str) -> list[str]:
    m = _PAREN.match(title.strip())
    return [m.group("a").strip(), m.group("b").strip()] if m else [title.strip()]


def build_aliases(onet_csv: Path, overrides: Path, out: Path) -> int:
    ov = yaml.safe_load(overrides.read_text(encoding="utf-8")) if overrides.exists() else {}
    table: dict[str, dict] = {}
    with onet_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = row["O*NET-SOC Code"]
            entry = table.setdefault(code, {"label": row["Title"], "aliases": set(), "acronyms": [], "related": []})
            entry["aliases"].update(_split(row["Job Title"]))
    for code, o in (ov or {}).items():
        entry = table.setdefault(code, {"label": o.get("label", code), "aliases": set(), "acronyms": [], "related": []})
        entry["aliases"].update(o.get("add", []))
        entry["aliases"].difference_update(o.get("drop", []))
        entry["acronyms"] = sorted(set(o.get("acronyms", [])))
        entry["related"] = list(o.get("related", []))
    final = {c: {**e, "aliases": sorted(e["aliases"])} for c, e in sorted(table.items())}
    out.write_text(yaml.safe_dump(final, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return len(final)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--onet", type=Path, required=True, help=f"job_titles.csv from {ONET_URL}")
    p.add_argument("--overrides", type=Path, default=Path("data/title_aliases.overrides.yaml"))
    p.add_argument("--out", type=Path, default=Path("src/ergon/query/title_aliases.yaml"))
    a = p.parse_args()
    sys.exit(0 if build_aliases(a.onet, a.overrides, a.out) else 1)
```

`data/title_aliases.overrides.yaml` initial content:

```yaml
"15-1252.00":
  acronyms: [SWE, SDE, SDET]
  related:
    - {code: "15-2051.00", reason: "ML engineers are hired and titled as software roles"}
    - {code: "15-1221.00", reason: "O*NET files Machine Learning Engineer here"}
"15-2051.00":
  acronyms: [DS, MLE]
"15-1243.00":
  acronyms: [DE]
"15-1244.00":
  acronyms: [SRE, DevOps]
"11-2021.00":
  acronyms: [PM]
"15-1253.00":
  acronyms: [QA, SDET]
```

Generate: download the O*NET CSV (URL above; ~5 MB), run the script, commit `src/ergon/query/title_aliases.yaml`. Add the O*NET paragraph to `NOTICE`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_build_title_aliases.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/build_title_aliases.py data/title_aliases.overrides.yaml src/ergon/query/title_aliases.yaml tests/fixtures/onet_job_titles_slice.csv tests/test_build_title_aliases.py NOTICE pyproject.toml
git commit -m "feat(query): title alias table generated from O*NET 30.3 with curated acronyms and cross-links"
```

---

### Task 15: Alias lookup and query-time expansion; eval run

**Files:**
- Create: `src/ergon/query/aliases.py`
- Modify: `src/ergon/index/query.py:_query_match` (replace the `_ML_ALIAS` special case), `scripts/search_eval/configs.py` (add `aliases` config building on `understand`)
- Test: `tests/query/test_aliases.py`, extend `tests/test_search_adversarial_eval.py` cases that relied on `_ML_ALIAS`

**Interfaces:**
- Produces: `lookup(role: str) -> Match | None` with `Match(code: str, label: str, matched: str)`; `expansions(role: str, *, cap: int = 12, related_top: int = 3) -> list[str]` (alias variants excluding the role itself, ordered by O*NET row order then related); `_query_match(q)` uses `expansions(q.keywords)` when `q.aliases` resolves true (explicit or `default_for("aliases")`), each variant as an OR arm restricted to the title column (`title:"…"` FTS5 column filter) with `allow_any=False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/query/test_aliases.py
from ergon.query.aliases import expansions, lookup


def test_lookup_is_case_and_plural_insensitive():
    assert lookup("Software Engineers").code == "15-1252.00"
    assert lookup("software-engineer").code == "15-1252.00"


def test_acronyms_match_as_whole_tokens_only():
    assert lookup("SWE").code == "15-1252.00"
    assert lookup("answer") is None and lookup("rpm") is None


def test_collision_acronym_yields_both():
    ex = expansions("SRE")
    assert any("Site Reliability" in e for e in ex)
    assert lookup("SRE") is not None


def test_expansion_is_capped_and_excludes_self():
    ex = expansions("software engineer", cap=5)
    assert len(ex) <= 5 + 3 * 2 and "software engineer" not in {e.lower() for e in ex}


def test_mle_reaches_software_developers_via_related():
    ex = {e.lower() for e in expansions("ML engineer")}
    assert "software engineer" in ex


def test_no_match_no_expansion():
    assert expansions("underwater basket weaver") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/query/test_aliases.py -v`
Expected: FAIL (`No module named 'ergon.query.aliases'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/ergon/query/aliases.py
"""Title alias lookup and bounded query-time expansion over the O*NET-derived table."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

import yaml


@dataclass(frozen=True)
class Match:
    code: str
    label: str
    matched: str


def _norm(s: str) -> str:
    s = re.sub(r"[-_/]+", " ", s.lower()).strip()
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"s\b", "", s) if s.endswith("s") and not s.endswith("ss") else s


@lru_cache(maxsize=1)
def _table() -> tuple[dict, dict[str, list[str]], dict[str, list[str]]]:
    data = yaml.safe_load(files("ergon.query").joinpath("title_aliases.yaml").read_text(encoding="utf-8"))
    by_alias: dict[str, list[str]] = {}
    by_acr: dict[str, list[str]] = {}
    for code, e in data.items():
        for a in e["aliases"]:
            by_alias.setdefault(_norm(a), []).append(code)
        for a in e.get("acronyms", []):
            by_acr.setdefault(a.upper(), []).append(code)
    return data, by_alias, by_acr


def _codes(role: str) -> list[str]:
    data, by_alias, by_acr = _table()
    toks = re.findall(r"[A-Za-z][A-Za-z+#.]*", role)
    if len(toks) == 1 and toks[0].upper() in by_acr and (toks[0].isupper() or len(toks[0]) <= 4):
        return by_acr[toks[0].upper()]
    key = _norm(role)
    if key in by_alias:
        return by_alias[key]
    # "ML engineer": expand a leading acronym token and retry
    if toks and toks[0].upper() in by_acr:
        return by_acr[toks[0].upper()]
    return []


def lookup(role: str) -> Match | None:
    codes = _codes(role)
    if not codes:
        return None
    data = _table()[0]
    return Match(codes[0], data[codes[0]]["label"], role)


def expansions(role: str, *, cap: int = 12, related_top: int = 3) -> list[str]:
    data = _table()[0]
    codes = _codes(role)
    if not codes:
        return []
    out: list[str] = []
    seen = {_norm(role)}
    for code in codes:
        for a in data[code]["aliases"][:cap]:
            if _norm(a) not in seen:
                out.append(a)
                seen.add(_norm(a))
        for rel in data[code].get("related", []):
            for a in data.get(rel["code"], {}).get("aliases", [])[:related_top]:
                if _norm(a) not in seen:
                    out.append(a)
                    seen.add(_norm(a))
    return out[: cap + related_top * 3]
```

In `query.py`, replace the `_ML_ALIAS` block of `_query_match` with:

```python
    from ..query.aliases import expansions
    from ..query.defaults import default_for

    on = q.aliases if q.aliases is not None else default_for("aliases")
    variants = [q.keywords]
    if on:
        variants += [f"title:{v}" for v in expansions(q.keywords)]
```

and make `_match_expr` accept a `title:` prefix: when `keywords.startswith("title:")`, build the phrase/NEAR expression over the remaining text and prefix each quoted term with `title:` (FTS5 column filter), always with `allow_any=False`. Keep `q.semantic` behaviour identical when `on` is false. Update `tests/test_search_adversarial_eval.py`: the ML cases pass `aliases=True` instead of relying on `semantic=True` for expansion (and keep the `semantic=True` expectations by setting `aliases=True` alongside). `configs.py`: `CONFIGS["aliases"] = Config("aliases", lambda t: SearchQuery(keywords=t, understand=True, aliases=True))`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/query tests/test_search_adversarial_eval.py tests/test_query*.py tests/test_revalidation_hardening.py -q`
Expected: PASS

- [ ] **Step 5: Eval run and commit**

Run `aliases` config through run → pool → judge → adjudicate → qrels → `report --default understand --candidate aliases` (or `--default bm25` if `understand` did not clear its gate). Record the MLE↔SWE before/after in the PR body. Commit code and data:

```bash
git add src/ergon tests scripts/search_eval/configs.py data/search_eval
git commit -m "feat(query): title alias expansion on the title field; eval run 'aliases' and its gate report"
```

---

## Phase D — Reranker

### Task 16: Reranker backends and the `[rerank]` extra

**Files:**
- Create: `src/ergon/rerank.py`
- Modify: `pyproject.toml` (`rerank = ["fastembed>=0.3"]`)
- Test: `tests/test_rerank.py`

**Interfaces:**
- Consumes: `ergon.ranking.Reranker` protocol (`rerank(query, jobs) -> list[float]`), `JobPosting`.
- Produces: `doc_text(job: JobPosting) -> str` (`"{title} · {company} · {location} · {snippet}"`, snippet last); `CrossEncoderReranker(model: str = DEFAULT_MODEL, *, backend: str = "fastembed")` implementing `rerank`; `LateInteractionReranker(model: str)` (fastembed `LateInteractionTextEmbedding` + MaxSim); `SentenceTransformersReranker(model: str)` (eval-only; imports lazily); `get_reranker(model: str | None = None) -> Reranker` selecting backend by model prefix (`st:` → sentence-transformers, `colbert:` → late interaction, else fastembed cross-encoder); `DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"`, env `ERGON_RERANK_MODEL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rerank.py
import pytest

from ergon.models import JobPosting, Location
from ergon.rerank import CrossEncoderReranker, doc_text, get_reranker


def _job(title, snippet=""):
    j = JobPosting.create(source="greenhouse", source_job_id=title, company="Acme", title=title)
    j.locations = [Location(raw="Austin, TX", city="Austin", country="US")]
    j.snippet = snippet
    return j


def test_doc_text_puts_title_first_and_snippet_last():
    t = doc_text(_job("SWE", "long snippet"))
    assert t.startswith("SWE · Acme · Austin") and t.endswith("long snippet")


class _FakeEncoder:
    def __init__(self, *a, **k): pass
    def rerank(self, query, documents):
        return [1.0 if "Machine Learning" in d else 0.1 for d in documents]


def test_cross_encoder_scores_in_input_order(monkeypatch):
    import ergon.rerank as r
    monkeypatch.setattr(r, "_load_cross_encoder", lambda model: _FakeEncoder())
    rr = CrossEncoderReranker("fake")
    jobs = [_job("Nurse"), _job("Machine Learning Engineer")]
    assert rr.rerank("ML engineer", jobs) == [0.1, 1.0]


def test_backend_selection_by_prefix():
    assert type(get_reranker("colbert:answerdotai/answerai-colbert-small-v1")).__name__ == "LateInteractionReranker"
    assert type(get_reranker("st:jhu-clsp/ettin-reranker-17m")).__name__ == "SentenceTransformersReranker"
    assert type(get_reranker("Xenova/ms-marco-MiniLM-L-6-v2")).__name__ == "CrossEncoderReranker"


def test_missing_extra_is_a_clear_error(monkeypatch):
    import ergon.rerank as r
    def boom(model):
        raise ImportError("No module named fastembed")
    monkeypatch.setattr(r, "_load_cross_encoder", boom)
    with pytest.raises(RuntimeError, match=r"pip install 'ergon\[rerank\]'"):
        CrossEncoderReranker("x").rerank("q", [_job("a")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rerank.py -v`
Expected: FAIL (`No module named 'ergon.rerank'`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/ergon/rerank.py
"""Cross-encoder / late-interaction rerankers over the BM25 head. Optional extra: ergon[rerank]."""

from __future__ import annotations

import os
from typing import Any

from .models import JobPosting

DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
_HINT = "install the reranker extra: pip install 'ergon[rerank]'"


def doc_text(job: JobPosting) -> str:
    loc = job.locations[0].raw if job.locations else ""
    return " · ".join(x for x in (job.title, job.company, loc, job.snippet or "") if x)


def _load_cross_encoder(model: str) -> Any:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=model)


class CrossEncoderReranker:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model, self._enc = model, None

    def rerank(self, query: str, jobs: list[JobPosting]) -> list[float]:
        if self._enc is None:
            try:
                self._enc = _load_cross_encoder(self._model)
            except ImportError as exc:
                raise RuntimeError(f"reranker model {self._model!r} unavailable: {_HINT}") from exc
        return [float(s) for s in self._enc.rerank(query, [doc_text(j) for j in jobs])]


class LateInteractionReranker:
    def __init__(self, model: str) -> None:
        self._model, self._enc = model, None

    def rerank(self, query: str, jobs: list[JobPosting]) -> list[float]:
        import numpy as np

        if self._enc is None:
            try:
                from fastembed import LateInteractionTextEmbedding

                self._enc = LateInteractionTextEmbedding(model_name=self._model)
            except ImportError as exc:
                raise RuntimeError(f"reranker model {self._model!r} unavailable: {_HINT}") from exc
        q = next(iter(self._enc.query_embed([query])))
        docs = list(self._enc.embed([doc_text(j) for j in jobs]))
        return [float(np.max(np.asarray(q) @ np.asarray(d).T, axis=1).sum()) for d in docs]


class SentenceTransformersReranker:  # eval-only: never in an install extra
    def __init__(self, model: str) -> None:
        self._model, self._enc = model, None

    def rerank(self, query: str, jobs: list[JobPosting]) -> list[float]:
        if self._enc is None:
            from sentence_transformers import CrossEncoder

            self._enc = CrossEncoder(self._model)
        return [float(s) for s in self._enc.predict([(query, doc_text(j)) for j in jobs])]


def get_reranker(model: str | None = None) -> Any:
    model = model or os.environ.get("ERGON_RERANK_MODEL") or DEFAULT_MODEL
    if model.startswith("st:"):
        return SentenceTransformersReranker(model[3:])
    if model.startswith("colbert:"):
        return LateInteractionReranker(model[8:])
    return CrossEncoderReranker(model)
```

`pyproject.toml`: add `rerank = ["fastembed>=0.3"]` under `[project.optional-dependencies]`, and `numpy` is already pulled by fastembed.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rerank.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ergon/rerank.py tests/test_rerank.py pyproject.toml uv.lock
git commit -m "feat(rerank): pluggable reranker backends behind the [rerank] extra"
```

---

### Task 17: Wire the reranker with depth, budget and block ordering; eval runs

**Files:**
- Modify: `src/ergon/ranking.py:rank` (add `top_k: int = 100` parameter; keep the reranked block above the tail), `src/ergon/index/router.py:try_index_ranked` (apply the reranker when `query.rerank` resolves true), `src/ergon/cli.py` (`--rerank/--no-rerank`, `--explain` shows reranker depth/model/latency), `src/ergon/mcp_server.py` (`rerank` parameter), `scripts/search_eval/configs.py` (`rerank` config; model via `ERGON_RERANK_MODEL`)
- Test: `tests/test_rank_block_order.py`, `tests/test_router_rerank.py`

**Interfaces:**
- Produces: `rank(jobs, query, *, reranker=None, top_k: int = 100)`; after reranking, every reranked job's score is offset so the block sorts strictly above the tail: `job.score = max_tail_score + 1.0 + rerank_score_rank_position` (i.e. block order by reranker score, then the tail in BM25 order). `router.try_index_ranked`: when rerank is on and `query.keywords`, fetch the pool (`max(want*10, 200)` as today), apply `rank(pool, role_phrase, reranker=get_reranker(), top_k=depth)` where `depth` = `ERGON_RERANK_DEPTH` (default 50) reduced by calibration to stay under `ERGON_RERANK_MAX_MS` (default 400): on first use time a 10-pair batch, extrapolate linearly, and cap depth. Any failure → warning once → lexical order.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rank_block_order.py
from ergon.models import JobPosting
from ergon.ranking import rank


class _Rev:
    def rerank(self, query, jobs):
        return [float(i) for i in range(len(jobs))]  # reverse the head


def test_reranked_block_stays_above_the_tail():
    jobs = [JobPosting.create(source="s", source_job_id=str(i), company="c", title=f"engineer {i}") for i in range(6)]
    out = rank(jobs, "engineer", reranker=_Rev(), top_k=3)
    head, tail = out[:3], out[3:]
    assert {j.title for j in head} == {"engineer 0", "engineer 1", "engineer 2"}, "the head is the BM25 top-3"
    assert [j.title for j in tail] == ["engineer 3", "engineer 4", "engineer 5"], "the tail keeps BM25 order"
```

```python
# tests/test_router_rerank.py
from ergon.index import router
from ergon.models import JobPosting, SearchQuery


def test_rerank_flag_routes_through_the_reranker(monkeypatch):
    jobs = [JobPosting.create(source="s", source_job_id=str(i), company="c", title=t) for i, t in enumerate(["Nurse", "ML Engineer"])]
    monkeypatch.setattr(router, "try_index", lambda q: list(jobs))
    calls = []

    class _R:
        def rerank(self, query, js):
            calls.append(query)
            return [0.0 if j.title == "Nurse" else 1.0 for j in js]

    monkeypatch.setattr("ergon.rerank.get_reranker", lambda model=None: _R())
    out = router.try_index_ranked(SearchQuery(keywords="ML engineer", rerank=True, limit=2))
    assert [j.title for j in out] == ["ML Engineer", "Nurse"] and calls == ["ML engineer"]


def test_rerank_failure_falls_back_to_lexical(monkeypatch, caplog):
    jobs = [JobPosting.create(source="s", source_job_id="1", company="c", title="Nurse")]
    monkeypatch.setattr(router, "try_index", lambda q: list(jobs))
    def boom(model=None):
        raise RuntimeError("no model")
    monkeypatch.setattr("ergon.rerank.get_reranker", boom)
    out = router.try_index_ranked(SearchQuery(keywords="nurse", rerank=True))
    assert [j.title for j in out] == ["Nurse"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rank_block_order.py tests/test_router_rerank.py -v`
Expected: FAIL (`rank() got an unexpected keyword argument 'top_k'`; router ignores `rerank`)

- [ ] **Step 3: Write minimal implementation**

In `ranking.py`, change the signature to `def rank(jobs, query, *, reranker=None, top_k: int = 100)` and replace the reranker block with:

```python
    active = reranker if reranker is not None else _RERANKER
    if active is not None and query:
        k = min(len(jobs), top_k)
        order = sorted(range(len(jobs)), key=lambda i: scores[i], reverse=True)
        head = [jobs[i] for i in order[:k]]
        tail = [jobs[i] for i in order[k:]]
        try:
            re_scores = active.rerank(query, head)
            base = (max((j.score or 0.0) for j in tail) if tail else 0.0) + 1.0
            for pos, (job, sc) in enumerate(sorted(zip(head, re_scores, strict=True), key=lambda p: p[1], reverse=True)):
                job.score = base + (len(head) - pos)  # block strictly above the tail, ordered by reranker
        except Exception:  # noqa: BLE001 - a reranker failure must not break search
            pass
```

In `router.py`, inside `try_index_ranked` after the semantic block:

```python
    on = query.rerank if query.rerank is not None else default_for("rerank")
    if on and query.keywords and len(indexed) > 1:
        try:
            from ..ranking import rank
            from ..rerank import get_reranker

            want = query.limit or 20
            pool = try_index(query.model_copy(update={"limit": max(want * 10, 200)})) or indexed
            indexed = rank(pool, query.keywords, reranker=get_reranker(), top_k=_rerank_depth(len(pool)))[:want]
        except Exception as exc:  # noqa: BLE001 - reranker optional; lexical order is fine
            log.warning("reranker unavailable (%s); lexical order", exc)
    return indexed
```

with `from ..query.defaults import default_for` at module top and:

```python
_CALIBRATED_MS_PER_PAIR: float | None = None


def _rerank_depth(pool_size: int) -> int:
    depth = int(os.environ.get("ERGON_RERANK_DEPTH", "50"))
    budget = float(os.environ.get("ERGON_RERANK_MAX_MS", "400"))
    if _CALIBRATED_MS_PER_PAIR:
        depth = min(depth, max(5, int(budget / _CALIBRATED_MS_PER_PAIR)))
    return min(depth, pool_size)
```

Calibration: the first call times `get_reranker().rerank(...)` on the first 10 pairs (inside the `try`) and sets `_CALIBRATED_MS_PER_PAIR` to elapsed/10; `--explain` prints depth, model and the calibrated figure. CLI: `--rerank/--no-rerank` → `SearchQuery.rerank`. MCP: `rerank: bool | None = None` parameter. `configs.py`: `CONFIGS["rerank"] = Config("rerank", lambda t: SearchQuery(keywords=t, understand=True, aliases=True, rerank=True))` — note `run_config` calls `search_rows` directly, which bypasses the router; add a `via_router: bool` field to `Config` (default False) and, when true, have `run_config` call `router.try_index_ranked(sq)` against a backend opened on the pinned index (`ergon.index.backend.SqliteIndexBackend(index)` set via `monkeypatch`-free injection: `router._load_backend` reads `ERGON_INDEX_PATH`; set it in `run_config` when `via_router`). Score = `depth - rank`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rank_block_order.py tests/test_router_rerank.py tests/test_ranking*.py tests/test_router*.py -q`
Expected: PASS

- [ ] **Step 5: Eval runs and commit**

For each model — `Xenova/ms-marco-MiniLM-L-6-v2`, `st:jhu-clsp/ettin-reranker-17m`, `st:jhu-clsp/ettin-reranker-32m`, `colbert:answerdotai/answerai-colbert-small-v1` (install `sentence-transformers` ad hoc for the `st:` runs; verify the ColBERT model id in `LateInteractionTextEmbedding.list_supported_models()` first and skip if absent) — run `ERGON_RERANK_MODEL=<model> uv run python -m search_eval run --config rerank --index dist/index.sqlite` writing `runs/rerank-<slug>.trec` (add `--name` to the `run` subcommand for this), pool/judge/adjudicate once for the union, then `report --default aliases --candidate rerank-<slug> --n-tests 4` for each, plus one offline `st:zeroentropy/zerank-2` run as the ceiling (not gated). Record p50/p95 latency at k=50 from `--explain` on 20 queries. Ship the best model that passes the gate and stays under 400 ms; if it is an Ettin model, export to ONNX with `optimum-cli export onnx --model jhu-clsp/ettin-reranker-<n> …`, re-run the gate on the exported model via the fastembed backend (custom model path), and only then set `DEFAULT_MODEL`. Commit:

```bash
git add src/ergon tests scripts/search_eval data/search_eval
git commit -m "feat(rerank): rerank the BM25 head under a latency budget; eval runs per model and the shipping decision"
```

---

## Phase E — Dense fusion, measured last

### Task 18: `dense` run with tuned fusion; final defaults

**Files:**
- Modify: `scripts/search_eval/configs.py` (`dense` config: `SearchQuery(keywords=t, understand=True, aliases=True, rerank=<shipped>, semantic=True)` via router), `scripts/search_eval/report.py` (add `fuse_runs(qrels, lexical: Path, dense: Path, out: Path) -> dict` using `ranx.optimize_fusion` with `method="wsum"` on a 50/50 query split: tune on half, evaluate on the other half, then write the fused run), `data/search_eval/defaults.yaml`, `README.md` (release rules paragraph)
- Test: `tests/search_eval/test_fusion.py`

**Interfaces:**
- Produces: `fuse_runs(...)` returning `{"weights": [...], "tune_ndcg": float, "holdout_ndcg": float}`; the fused run file for the gate.

- [ ] **Step 1: Write the failing test**

```python
# tests/search_eval/test_fusion.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from search_eval.report import fuse_runs, load_qrels  # noqa: E402
from search_eval.run import write_trec  # noqa: E402


def test_fusion_tunes_on_half_and_reports_holdout(tmp_path):
    q = tmp_path / "qrels.tsv"
    q.write_text("".join(f"q{i}\tx{i}\t3\thuman\nq{i}\ty{i}\t1\tllm\n" for i in range(8)))
    lex = tmp_path / "lex.trec"; den = tmp_path / "den.trec"
    write_trec({f"q{i}": [(f"x{i}", 2.0), (f"y{i}", 1.0)] for i in range(8)}, lex, "lex")
    write_trec({f"q{i}": [(f"y{i}", 2.0), (f"x{i}", 1.0)] for i in range(8)}, den, "den")
    g, _ = load_qrels(q)
    out = tmp_path / "fused.trec"
    res = fuse_runs(g, lex, den, out)
    assert out.exists() and len(res["weights"]) == 2
    assert 0.0 <= res["holdout_ndcg"] <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/search_eval/test_fusion.py -v`
Expected: FAIL (`cannot import name 'fuse_runs'`)

- [ ] **Step 3: Write minimal implementation**

Append to `report.py`:

```python
def fuse_runs(qrels: Qrels, lexical: Path, dense: Path, out: Path) -> dict[str, Any]:
    from ranx import fuse, optimize_fusion

    runs = [Run.from_file(str(lexical), kind="trec"), Run.from_file(str(dense), kind="trec")]
    qids = sorted(qrels.qrels)
    tune, hold = qids[::2], qids[1::2]

    def subset(q: Qrels, keep: list[str]) -> Qrels:
        return Qrels({k: v for k, v in q.qrels.items() if k in keep})

    def subset_run(r: Run, keep: list[str]) -> Run:
        rr = Run({k: v for k, v in r.run.items() if k in keep}); rr.name = r.name; return rr

    params = optimize_fusion(subset(qrels, tune), [subset_run(r, tune) for r in runs], norm="min-max", method="wsum", metric="ndcg@10")
    fused_hold = fuse([subset_run(r, hold) for r in runs], norm="min-max", method="wsum", params=params)
    hold_score = evaluate(subset(qrels, hold), fused_hold, "ndcg@10")
    fused_all = fuse(runs, norm="min-max", method="wsum", params=params)
    fused_all.name = "dense"
    fused_all.save(str(out), kind="trec")
    tune_score = evaluate(subset(qrels, tune), fuse([subset_run(r, tune) for r in runs], norm="min-max", method="wsum", params=params), "ndcg@10")
    return {"weights": list(params.get("weights", [])), "tune_ndcg": float(tune_score), "holdout_ndcg": float(hold_score)}
```

`configs.py`: `CONFIGS["dense"]` builds on the shipped defaults with `semantic=True`, `via_router=True`. Procedure: run `dense` (embedding rerank via the existing `_vector_rerank`) as its own run file, then `fuse_runs` against the current default run, then `report --default <current default> --candidate dense`. Write the final `defaults.yaml` (and the packaged copy) with `write_defaults` for every step; add the release-rules paragraph to `README.md` (default flip = minor version with the report linked; filters-from-free-text called out explicitly).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/search_eval -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/search_eval data/search_eval src/ergon/query/defaults.yaml README.md tests/search_eval/test_fusion.py
git commit -m "feat(search-eval): dense fusion measured under the same gate; final defaults and release rules"
```

---

## Self-review

**Spec coverage.** §3 harness → Tasks 1–8 (query set, pooling, UMBRELA judging with the expect check, auditor, TREC qrels with `source`, `ranx` metrics, per-slice, unjudged rate, build pinning, CLI, CI regression). §4 understand → Tasks 9–13 (salary with period and traps; experience with years-over-level; GeoNames gazetteer with attribution; employment; assembly with explicit-wins, explain, miss log, never-raises; wiring into engine/MCP/CLI; `understand` flag). §5 aliases → Tasks 14–15 (O*NET generator with parenthetical split, overrides with acronyms/related/drop, MLE↔SWE row, SRE collision, cap 12/related 3, title-column arms with `allow_any=False`, explain via `lookup`). §6 reranker → Tasks 16–17 (protocol reuse, three backends, `[rerank]` extra, depth 50/100, 400 ms budget with calibration, block-above-tail, doc text order, fallback, clear error, per-model eval, zerank-2 ceiling, ONNX export rule). §7 gating → Task 6 gate (paired t, Bonferroni, per-slice tolerance, unjudged < 10%, latency reported), Task 7 exit code, Task 18 fusion via `optimize_fusion` and the final defaults; release rules in README (Task 18). Approach-B trigger → Task 13 (`oracle` config and the parser-coverage lines in `report`). §8 out of scope: respected.

**Placeholder scan.** No TBD/TODO. Task 5's auditor is specified by reference to an existing file plus the exact fields and output format, which is the intended level for a copied UI. Task 8 is a human procedure with exact commands.

**Type consistency.** `Span` defined in `salary.py` and imported by the other extractors; `Parse.spans: dict[str, str]`; `Config(name, build, via_router=False)` is defined in Task 2 and used by Tasks 17–18; `write_trec(run, path, name)` used identically in Tasks 2, 6, 18; `RunReport.run` is set by `evaluate_run` and required by `gate`; `default_for(step)` used in Tasks 13, 15, 17.
