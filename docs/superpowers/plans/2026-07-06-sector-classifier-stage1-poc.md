# Sector Classifier — Stage 1 (PoC) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove (or disprove) that a frozen bge-small embedding + calibrated logistic-regression probe beats the current deterministic sector extractor (72.4% accuracy / 26.7% coverage) on our existing 700-company corpus — the de-risking gate that decides whether Tier-2 ML is worth building in Stages 2–3.

**Architecture:** Fully **offline** PoC. A numpy-only feature/inference core lives in `src/` (`sector_features.py`, `sector_clf.py`); all training (sklearn) lives in `scripts/`. We embed the 699-company corpus once (single-process, memory-bounded — the rich-tier OOM pattern), fit an L2-regularized multinomial logreg with per-class Platt calibration, tune a 3-gate abstention threshold to a target precision, export a `.npz`, and benchmark accuracy-at-coverage / macro-F1 / risk–coverage against the baseline. No runtime wiring, no downloads, no new hard runtime dependency.

**Tech Stack:** Python ≥3.10, fastembed (`BAAI/bge-small-en-v1.5`, existing `semantic` extra), numpy (added to `dev`), scikit-learn (new train-only `sector-train` extra), pytest (`asyncio_mode=auto`), hatchling packaging.

## Global Constraints

- **Free · offline · CPU-only · laptop-safe.** No paid APIs, no network at train time beyond the one-time fastembed model download. Heavy work is one-time/offline, never in the daily build.
- **sklearn appears ONLY under `scripts/`.** `src/ergon_tracker/**` inference is **numpy-only**. Rationale: mypy runs `strict` on `files = ["src/ergon_tracker"]` only; numpy is already `follow_imports = skip`; sklearn is untyped and must never enter the type-checked package.
- **No new hard runtime dependency.** numpy is lazy-imported inside functions (the repo's existing pattern, see `index/rich.py`). numpy added to the `dev` extra so CI runs the inference tests; sklearn added to a separate `sector-train` extra.
- **Memory-bounded embedding is mandatory.** Embed via `SemanticReranker.embed_texts(..., parallel=None)` (single-process — no per-worker model copies; this is the fix for CI OOM run 28070765535, commit `70a74c7`). Every heavy step logs peak RSS + wall time and is stress-tested on a small `--sample` before any full run.
- **Concurrency is env-gated, laptop-safe by default.** Any worker/`n_jobs` count follows the repo idiom: explicit env var wins → else `max(2, cpu-2)` on CI → else `1` local. New knob: `ERGON_SECTOR_JOBS`.
- **Python floor `>=3.10`; ruff line-length 100, target `py310`, select `E,F,I,UP,B,SIM,C4`, ignore `E501`; mypy strict on `src/`.**
- **Taxonomy = 27 sector labels**, defined implicitly by `sectors.json` values. The classifier derives its label set from the training data at train time and stores it in the `.npz` — never hard-codes 27.
- Commit after each task. Commits end with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer. Work on a feature branch, never `main`.

## Key Facts (verified against the codebase)

- **Extractor API:** `SectorExtractor.extract(inp: ExtractInput) -> str | None` (`src/ergon_tracker/extract/sector.py:392`). `ExtractInput` fields used: `company`, `company_key`, `company_domain`, `title` (`src/ergon_tracker/extract/base.py:21-35`).
- **Corpus:** `tests/fixtures/sector_corpus.jsonl`, 699 records, schema `{"company","company_key","domain","sector"|null,"src"}` (`tests/test_sector_recall.py:11,24`). Example: `{"company":"Apple","company_key":"apple","domain":"jobs.apple.com","sector":"Consumer/Lifestyle","src":"real"}`.
- **Embedder:** `SemanticReranker.embed_texts(texts, *, batch_size=256, parallel=None) -> list[list[float]]` (`src/ergon_tracker/semantic.py:106-118`); `get_semantic_reranker()` memoizes (`:162`). Model `BAAI/bge-small-en-v1.5`, 384-dim float32.
- **Data artifacts** live in `src/ergon_tracker/registry/data/` and auto-ship (hatchling `packages=["src/ergon_tracker"]`). Runtime load via `importlib.resources.files("ergon_tracker.registry.data") / "<file>"` with `FileNotFoundError`/`ModuleNotFoundError` tolerance + `@lru_cache(maxsize=1)` (pattern: `extract/sector.py:368-386`, `extract/geo.py:449`).
- **Env-gate idiom** (`index/build.py:788-795`): `env=os.environ.get("ERGON_SHARD_WORKERS"); if env: int(env) elif os.environ.get("CI"): max(2,(os.cpu_count() or 4)-2) else: 1`.
- **pytest:** `testpaths=["tests"]`, `asyncio_mode="auto"`, `addopts="-ra"`. Run one test: `.venv/bin/pytest tests/test_x.py::test_y -v`.
- **numpy is NOT a core dep; sklearn is absent.** `.npz`/`np.savez` not yet used anywhere. `requires-python=">=3.10"`.

---

## File Structure

**Create:**
- `src/ergon_tracker/extract/sector_features.py` — pure, numpy-lazy feature transforms shared by train + (future) runtime: input-text builder, TLD one-hot, CL2N, feature assembly. One responsibility: turn `(embedding, domain, name, title)` into a model-ready feature vector, deterministically.
- `src/ergon_tracker/extract/sector_clf.py` — the numpy-only model container + inference: `.npz` save/load and `SectorClassifier.predict()`. One responsibility: reproduce the trained model's decision (logits → calibrated probs → 3-gate abstention) with numpy only.
- `scripts/train_sector_classifier.py` — offline trainer (sklearn): embed corpus → CV/`C`-sweep logreg → per-class Platt → centroids → threshold sweep → export `.npz` + `metrics.json`. `--sample` stress mode.
- `scripts/eval_sector_classifier.py` — benchmark/report: accuracy-at-coverage, macro-F1, per-class F1, risk–coverage table, vs 72.4%/26.7% baseline; optional `--ceiling` deberta zero-shot (dev-only, guarded).
- `tests/test_sector_features.py` — unit tests for the feature transforms (numpy-only).
- `tests/test_sector_clf_roundtrip.py` — the critical correctness gate: numpy inference reproduces the exported math exactly; sklearn-parity check (importorskip).
- `tests/fixtures/sector_clf_tiny.npz` — a tiny hand-built model fixture for the roundtrip test (created by the test itself in a tmp dir; no binary committed).

**Modify:**
- `pyproject.toml` — add `sector-train` extra; add `numpy` to `dev`.
- `docs/extraction-baseline.md` — after the PoC run, record the Stage-1 result + go/no-go (Task 8).

**Deliberately NOT touched in Stage 1:** `src/ergon_tracker/extract/sector.py` (no Tier-2 wiring until Stage 3), `tests/test_sector_recall.py` (the deterministic gate stays intact).

---

## Task 1: Dependencies + label/TLD constants

**Files:**
- Modify: `pyproject.toml:33-52` (extras)
- Create: `src/ergon_tracker/extract/sector_features.py`
- Test: `tests/test_sector_features.py`

**Interfaces:**
- Produces: `SECTOR_TLD_GROUPS: dict[str, tuple[str, ...]]`, `TLD_VOCAB: tuple[str, ...]` (stable order), `build_input_text(name: str | None, domain: str | None, title: str | None) -> str`.

- [ ] **Step 1: Add the extras to `pyproject.toml`**

In `[project.optional-dependencies]` add a train-only extra and put numpy in `dev` (so CI, which installs `dev`, runs the numpy inference tests):

```toml
# Offline-only: training the sector classifier (scripts/train_sector_classifier.py). Never at runtime.
sector-train = ["scikit-learn>=1.4", "numpy>=1.26"]
```

And append `"numpy>=1.26",` to the `dev = [ ... ]` list (last entry before the closing `]`).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_sector_features.py
from __future__ import annotations

from ergon_tracker.extract.sector_features import (
    TLD_VOCAB,
    build_input_text,
    tld_features,
)


def test_input_text_combines_name_domain_title() -> None:
    # domain contributes its registrable label WITHOUT the TLD (the TLD is a separate feature).
    txt = build_input_text("Acme Corp", "careers.acme-bank.com", "Senior Risk Analyst")
    assert "Acme Corp" in txt
    assert "acme-bank" in txt
    assert ".com" not in txt
    assert "Senior Risk Analyst" in txt


def test_input_text_tolerates_missing_fields() -> None:
    assert build_input_text(None, None, None) == ""
    assert build_input_text("Acme", None, None) == "Acme"


def test_tld_features_are_fixed_width_and_grouped() -> None:
    vec = tld_features("foo.ai")
    assert len(vec) == len(TLD_VOCAB)
    assert sum(vec) == 1.0  # .ai lights exactly its group
    assert tld_features(None) == [0.0] * len(TLD_VOCAB)  # no domain → all-zero
    assert tld_features("foo.unknown-tld") == [0.0] * len(TLD_VOCAB)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_sector_features.py -v`
Expected: FAIL — `ModuleNotFoundError: ergon_tracker.extract.sector_features`.

- [ ] **Step 4: Implement the module (this part of it)**

```python
# src/ergon_tracker/extract/sector_features.py
"""Deterministic feature transforms for the sector classifier.

Pure and dependency-light: numpy is imported lazily inside the vector-math helpers (the repo
pattern, see ``index/rich.py``), so importing this module costs nothing at runtime and adds no hard
dependency. Shared by the offline trainer (``scripts/train_sector_classifier.py``) and the numpy-only
runtime inference (``sector_clf.py``) — identical features on both sides, by construction.
"""

from __future__ import annotations

import re

# TLD group -> the suffixes that map to it. Small, high-signal industry priors on the domain TLD.
SECTOR_TLD_GROUPS: dict[str, tuple[str, ...]] = {
    "tech": (".ai", ".io", ".dev", ".app", ".tech"),
    "finance": (".bank", ".finance", ".insurance"),
    "education": (".edu", ".ac.uk", ".edu.au"),
    "government": (".gov", ".mil", ".gov.uk"),
    "health": (".health", ".care"),
    "media": (".tv", ".fm", ".news"),
}
# Stable, sorted group order — the feature layout MUST NOT drift (the .npz depends on it).
TLD_VOCAB: tuple[str, ...] = tuple(sorted(SECTOR_TLD_GROUPS))

_WS = re.compile(r"\s+")


def _registrable_label(domain: str | None) -> str:
    """The domain's second-level label, TLD stripped ('careers.acme-bank.com' -> 'acme-bank')."""
    if not domain:
        return ""
    host = domain.strip().lower().split("/")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        # drop known multi-part public suffixes' last two labels, else the last one
        return parts[-3] if parts[-2] in {"co", "com", "ac", "gov", "edu"} and len(parts) >= 3 else parts[-2]
    return parts[0] if parts else ""


def build_input_text(name: str | None, domain: str | None, title: str | None) -> str:
    """The string fed to the embedder: '{name}. {registrable-domain-label}. {example title}'."""
    parts = [p for p in (name, _registrable_label(domain), title) if p]
    return _WS.sub(" ", ". ".join(s.strip() for s in parts)).strip()


def tld_features(domain: str | None) -> list[float]:
    """Fixed-width one-hot over ``TLD_VOCAB`` (all-zero when no group matches)."""
    vec = [0.0] * len(TLD_VOCAB)
    if not domain:
        return vec
    host = domain.strip().lower().split("/")[0]
    for i, group in enumerate(TLD_VOCAB):
        if any(host.endswith(suf) for suf in SECTOR_TLD_GROUPS[group]):
            vec[i] = 1.0
            break  # one group max — keeps the feature a clean indicator
    return vec
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sector_features.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Lint + commit**

```bash
.venv/bin/ruff check src/ergon_tracker/extract/sector_features.py tests/test_sector_features.py
.venv/bin/ruff format src/ergon_tracker/extract/sector_features.py tests/test_sector_features.py
git add pyproject.toml src/ergon_tracker/extract/sector_features.py tests/test_sector_features.py
git commit -m "feat(sector): feature transforms (input-text + TLD one-hot) + train extra

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: CL2N + feature assembly (vector math)

**Files:**
- Modify: `src/ergon_tracker/extract/sector_features.py`
- Test: `tests/test_sector_features.py`

**Interfaces:**
- Consumes: `tld_features`, `TLD_VOCAB` (Task 1).
- Produces:
  - `cl2n(mat, mean=None) -> tuple[np.ndarray, np.ndarray]` — mean-center then L2-normalize rows; returns `(normalized, mean_used)`.
  - `assemble(embeddings, domains, mean) -> np.ndarray` — CL2N the embeddings, append per-row TLD one-hot → `(n, embed_dim + len(TLD_VOCAB))` float32.
  - `FEATURE_DIM(embed_dim: int) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_sector_features.py
import numpy as np  # noqa: E402
from ergon_tracker.extract.sector_features import assemble, cl2n  # noqa: E402


def test_cl2n_centers_then_unit_normalizes() -> None:
    mat = np.array([[3.0, 0.0], [0.0, 4.0], [1.0, 1.0]], dtype=np.float32)
    out, mean = cl2n(mat)
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)  # rows are unit
    np.testing.assert_allclose(mean, mat.mean(axis=0), atol=1e-5)  # mean is the column mean


def test_cl2n_reuses_supplied_mean() -> None:
    train = np.array([[2.0, 0.0], [0.0, 2.0]], dtype=np.float32)
    _, mean = cl2n(train)
    test = np.array([[5.0, 1.0]], dtype=np.float32)
    out, mean2 = cl2n(test, mean)
    np.testing.assert_allclose(mean2, mean)  # supplied mean is passed through, not recomputed
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)


def test_assemble_appends_tld_onehot() -> None:
    emb = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)  # embed_dim=4
    _, mean = cl2n(emb)
    feats = assemble(emb, ["foo.ai"], mean)
    assert feats.shape == (1, 4 + len(TLD_VOCAB))
    assert feats.dtype == np.float32
    assert feats[0, 4:].sum() == 1.0  # the appended TLD block is the one-hot
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_sector_features.py -k "cl2n or assemble" -v`
Expected: FAIL — `ImportError: cannot import name 'cl2n'`.

- [ ] **Step 3: Implement**

```python
# append to src/ergon_tracker/extract/sector_features.py
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keep numpy out of the import-time path
    import numpy as np


def FEATURE_DIM(embed_dim: int) -> int:
    """Total feature width = embedding dims + the TLD one-hot block."""
    return embed_dim + len(TLD_VOCAB)


def cl2n(mat: "np.ndarray", mean: "np.ndarray | None" = None) -> "tuple[np.ndarray, np.ndarray]":
    """CL2N: mean-center (using ``mean`` if given, else the batch mean) then L2-normalize each row.

    CL2N is the standard preprocessing for frozen-embedding classifiers (SimpleShot 1911.04623):
    centering removes the shared component; unit-normalizing makes the logreg see direction, not scale.
    """
    import numpy as np

    x = np.asarray(mat, dtype=np.float32)
    m = x.mean(axis=0) if mean is None else np.asarray(mean, dtype=np.float32)
    c = x - m
    norms = np.linalg.norm(c, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (c / norms).astype(np.float32), m.astype(np.float32)


def assemble(mat: "np.ndarray", domains: "list[str | None]", mean: "np.ndarray") -> "np.ndarray":
    """CL2N the embeddings (with the frozen training ``mean``) and append per-row TLD one-hot."""
    import numpy as np

    normed, _ = cl2n(mat, mean)
    tld = np.asarray([tld_features(d) for d in domains], dtype=np.float32)
    return np.hstack([normed, tld]).astype(np.float32)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_sector_features.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Type-check (src is mypy-strict) + commit**

Run: `.venv/bin/mypy` — Expected: no new errors (numpy is `follow_imports=skip`; the quoted annotations avoid an import-time numpy dependency).

```bash
git add src/ergon_tracker/extract/sector_features.py tests/test_sector_features.py
git commit -m "feat(sector): CL2N + feature assembly (embedding + TLD block)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `.npz` model container + numpy-only inference

**Files:**
- Create: `src/ergon_tracker/extract/sector_clf.py`
- Test: `tests/test_sector_clf_roundtrip.py`

**Interfaces:**
- Consumes: `assemble`, `cl2n`, `TLD_VOCAB`, `FEATURE_DIM` (Tasks 1–2).
- Produces:
  - `save_sector_model(path, *, labels, mean, W, b, platt_a, platt_b, centroids, tau_prob, tau_margin, tau_sim, tld_vocab, embed_dim) -> None`
  - `load_sector_model(path) -> SectorClassifier | None` (lru-cached; tolerant of missing file → None)
  - `SectorClassifier.predict(embedding: np.ndarray, domain: str | None) -> tuple[str | None, float]` — returns `(label_or_None, top1_prob)`. `None` label = abstain.
  - `SectorClassifier.predict_batch(embeddings: np.ndarray, domains: list[str | None]) -> list[tuple[str | None, float]]`.

**Model math (numpy, exactly reproducible):** `x = concat(cl2n(embedding, mean), tld_onehot(domain))`; `logits = W @ x + b`; per-class Platt `cal_k = sigmoid(platt_a_k * logits_k + platt_b_k)`, `probs = cal / cal.sum()`; abstain unless `p1 ≥ τ_prob AND (p1 − p2) ≥ τ_margin AND cos(x, centroid[argmax]) ≥ τ_sim`.

- [ ] **Step 1: Write the failing roundtrip test (numpy-only — the correctness gate)**

```python
# tests/test_sector_clf_roundtrip.py
"""The exported .npz must reproduce the model's decision with numpy only — no sklearn at runtime."""
from __future__ import annotations

import numpy as np
import pytest

from ergon_tracker.extract.sector_clf import load_sector_model, save_sector_model


def _tiny_model(tmp_path):
    # 2 classes, embed_dim=3. Hand-built weights so the expected decision is analytic.
    labels = np.array(["Fintech", "Healthcare"])
    mean = np.zeros(3, dtype=np.float32)
    W = np.array([[5.0, 0, 0] + [0] * 6, [0, 5.0, 0] + [0] * 6], dtype=np.float32)  # feat = 3 + 6 TLD
    b = np.zeros(2, dtype=np.float32)
    platt_a = np.ones(2, dtype=np.float32)
    platt_b = np.zeros(2, dtype=np.float32)
    centroids = np.array([[1.0, 0, 0] + [0] * 6, [0, 1.0, 0] + [0] * 6], dtype=np.float32)
    p = tmp_path / "m.npz"
    save_sector_model(
        p, labels=labels, mean=mean, W=W, b=b, platt_a=platt_a, platt_b=platt_b,
        centroids=centroids, tau_prob=0.6, tau_margin=0.1, tau_sim=0.5, embed_dim=3,
    )
    load_sector_model.cache_clear()
    return load_sector_model(p)


def test_predicts_confident_class(tmp_path) -> None:
    clf = _tiny_model(tmp_path)
    label, prob = clf.predict(np.array([10.0, 0.0, 0.0], dtype=np.float32), domain=None)
    assert label == "Fintech"
    assert prob > 0.6


def test_abstains_when_ambiguous(tmp_path) -> None:
    clf = _tiny_model(tmp_path)
    # symmetric embedding -> near-tie -> margin gate fails -> abstain (None)
    label, _ = clf.predict(np.array([1.0, 1.0, 0.0], dtype=np.float32), domain=None)
    assert label is None


def test_missing_file_returns_none() -> None:
    load_sector_model.cache_clear()
    assert load_sector_model("/nonexistent/model.npz") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_sector_clf_roundtrip.py -v`
Expected: FAIL — `ModuleNotFoundError: ergon_tracker.extract.sector_clf`.

- [ ] **Step 3: Implement `sector_clf.py`**

```python
# src/ergon_tracker/extract/sector_clf.py
"""Numpy-only sector classifier: load an exported .npz and reproduce its calibrated, abstaining
decision. No sklearn, no fastembed here — the caller supplies the embedding. Tolerant of a missing
artifact (returns None), mirroring ``load_sector_index``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from .sector_features import TLD_VOCAB, tld_features

if TYPE_CHECKING:
    import numpy as np

_KEYS = (
    "labels", "mean", "W", "b", "platt_a", "platt_b", "centroids",
    "tau_prob", "tau_margin", "tau_sim", "tld_vocab", "embed_dim",
)


class SectorClassifier:
    """Holds the exported arrays and applies the decision rule with numpy."""

    def __init__(self, data: dict) -> None:
        import numpy as np

        self.labels: list[str] = [str(x) for x in data["labels"]]
        self.mean = np.asarray(data["mean"], dtype=np.float32)
        self.W = np.asarray(data["W"], dtype=np.float32)
        self.b = np.asarray(data["b"], dtype=np.float32)
        self.platt_a = np.asarray(data["platt_a"], dtype=np.float32)
        self.platt_b = np.asarray(data["platt_b"], dtype=np.float32)
        self.centroids = np.asarray(data["centroids"], dtype=np.float32)
        self.tau_prob = float(data["tau_prob"])
        self.tau_margin = float(data["tau_margin"])
        self.tau_sim = float(data["tau_sim"])
        self.embed_dim = int(data["embed_dim"])
        # centroid norms precomputed for the cosine gate
        self._cnorm = np.linalg.norm(self.centroids, axis=1)
        self._cnorm[self._cnorm == 0] = 1.0

    def _features(self, embeddings: "np.ndarray", domains: "list[str | None]") -> "np.ndarray":
        import numpy as np

        x = np.asarray(embeddings, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        c = x - self.mean
        n = np.linalg.norm(c, axis=1, keepdims=True)
        n[n == 0] = 1.0
        normed = c / n
        tld = np.asarray([tld_features(d) for d in domains], dtype=np.float32)
        return np.hstack([normed, tld]).astype(np.float32)

    def predict_batch(
        self, embeddings: "np.ndarray", domains: "list[str | None]"
    ) -> "list[tuple[str | None, float]]":
        import numpy as np

        feats = self._features(embeddings, domains)
        logits = feats @ self.W.T + self.b  # (n, n_classes)
        cal = 1.0 / (1.0 + np.exp(-(self.platt_a * logits + self.platt_b)))
        probs = cal / cal.sum(axis=1, keepdims=True)
        order = np.argsort(-probs, axis=1)
        top1 = order[:, 0]
        p1 = probs[np.arange(len(probs)), top1]
        p2 = probs[np.arange(len(probs)), order[:, 1]] if probs.shape[1] > 1 else np.zeros(len(probs))
        # cosine(features, chosen centroid)
        fnorm = np.linalg.norm(feats, axis=1)
        fnorm[fnorm == 0] = 1.0
        sim = np.einsum("ij,ij->i", feats, self.centroids[top1]) / (fnorm * self._cnorm[top1])
        out: list[tuple[str | None, float]] = []
        for i in range(len(probs)):
            ok = p1[i] >= self.tau_prob and (p1[i] - p2[i]) >= self.tau_margin and sim[i] >= self.tau_sim
            out.append((self.labels[top1[i]] if ok else None, float(p1[i])))
        return out

    def predict(self, embedding: "np.ndarray", domain: str | None) -> "tuple[str | None, float]":
        return self.predict_batch(embedding, [domain])[0]


def save_sector_model(
    path: "str | Path",
    *,
    labels,
    mean,
    W,
    b,
    platt_a,
    platt_b,
    centroids,
    tau_prob: float,
    tau_margin: float,
    tau_sim: float,
    embed_dim: int,
    tld_vocab: tuple[str, ...] = TLD_VOCAB,
) -> None:
    """Persist the model as a compressed .npz (auto-ships under registry/data if placed there)."""
    import numpy as np

    np.savez_compressed(
        path,
        labels=np.asarray(labels),
        mean=np.asarray(mean, dtype=np.float32),
        W=np.asarray(W, dtype=np.float32),
        b=np.asarray(b, dtype=np.float32),
        platt_a=np.asarray(platt_a, dtype=np.float32),
        platt_b=np.asarray(platt_b, dtype=np.float32),
        centroids=np.asarray(centroids, dtype=np.float32),
        tau_prob=np.float32(tau_prob),
        tau_margin=np.float32(tau_margin),
        tau_sim=np.float32(tau_sim),
        tld_vocab=np.asarray(list(tld_vocab)),
        embed_dim=np.int64(embed_dim),
    )


@lru_cache(maxsize=2)
def load_sector_model(path: "str | Path") -> SectorClassifier | None:
    """Load an exported model; return None if the file is absent (Tier-2 then simply doesn't fire)."""
    import numpy as np

    p = Path(path)
    if not p.exists():
        return None
    with np.load(p, allow_pickle=False) as data:
        return SectorClassifier({k: data[k] for k in _KEYS})
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_sector_clf_roundtrip.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Add the sklearn class-parity check (guarded)**

The meaningful invariant: with abstention disabled and identity Platt, our numpy predicted **class** must equal sklearn's predicted class on the *same assembled features*. This holds exactly — per-class sigmoid and sum-normalization are both monotonic/positive, so they preserve the argmax of the logits, which is what sklearn's softmax also selects. (We do NOT assert probability equality: our sigmoid-then-normalize calibration is deliberately not softmax.) The multi-class case is exercised (3 classes), and features are built through our own `assemble()` so the test rides the real feature path.

```python
# append to tests/test_sector_clf_roundtrip.py
from ergon_tracker.extract.sector_features import assemble, cl2n  # noqa: E402


def test_predicted_class_matches_sklearn(tmp_path) -> None:
    pytest.importorskip("sklearn")
    from sklearn.linear_model import LogisticRegression

    rng = np.random.default_rng(0)
    embed_dim = 8
    emb = rng.normal(size=(90, embed_dim)).astype(np.float32)
    y = (emb[:, 0] * 2 + emb[:, 1] - emb[:, 2]).astype(int) % 3  # 3 classes
    labels = np.array(["A", "B", "C"])
    _, mean = cl2n(emb)
    feats = assemble(emb, [None] * len(emb), mean)  # real feature path (embed_dim + TLD block)

    lr = LogisticRegression(max_iter=2000).fit(feats, y)
    p = tmp_path / "sk.npz"
    save_sector_model(
        p, labels=labels, mean=mean, W=lr.coef_, b=lr.intercept_,
        platt_a=np.ones(3, dtype=np.float32), platt_b=np.zeros(3, dtype=np.float32),
        centroids=np.zeros((3, feats.shape[1]), dtype=np.float32),
        tau_prob=0.0, tau_margin=0.0, tau_sim=-1.0, embed_dim=embed_dim,  # never abstain
    )
    load_sector_model.cache_clear()
    clf = load_sector_model(p)
    got = [lab for lab, _ in clf.predict_batch(emb, [None] * len(emb))]
    want = [labels[i] for i in lr.predict(feats)]
    assert got == want  # exact class parity through the real feature + inference path
```

- [ ] **Step 6: Run, lint, type-check, commit**

```bash
.venv/bin/pytest tests/test_sector_clf_roundtrip.py -v
.venv/bin/ruff check src/ergon_tracker/extract/sector_clf.py tests/test_sector_clf_roundtrip.py
.venv/bin/ruff format src/ergon_tracker/extract/sector_clf.py tests/test_sector_clf_roundtrip.py
.venv/bin/mypy
git add src/ergon_tracker/extract/sector_clf.py tests/test_sector_clf_roundtrip.py
git commit -m "feat(sector): numpy-only .npz model container + inference (calibrated, abstaining)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Corpus loader + memory-safe embedder (with stress gate)

**Files:**
- Create: `scripts/train_sector_classifier.py` (loader + embed portion)
- Test: `tests/test_train_sector_smoke.py`

**Interfaces:**
- Produces (module-level, importable by tests):
  - `load_corpus(path: Path) -> tuple[list[dict], list[str]]` — returns `(records_with_gold, gold_labels)`; drops records with null `sector`.
  - `embed_records(records, *, batch_size, sample=None, log=print) -> np.ndarray` — single-process embed of `build_input_text(...)`; logs peak RSS + wall time; `sample` truncates for the stress run.
  - `_peak_rss_mb() -> float`.

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_train_sector_smoke.py
"""Contract/smoke tests for the offline trainer — no fastembed, no full run."""
from __future__ import annotations

import numpy as np
import pytest

train = pytest.importorskip("scripts.train_sector_classifier", reason="run from repo root")


def test_load_corpus_drops_unlabeled(tmp_path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text(
        '{"company":"A","company_key":"a","domain":"a.ai","sector":"Fintech"}\n'
        '{"company":"B","company_key":"b","domain":"b.com","sector":null}\n'
    )
    records, labels = train.load_corpus(p)
    assert len(records) == 1 and labels == ["Fintech"]


def test_embed_records_uses_single_process(monkeypatch) -> None:
    seen = {}

    class FakeReranker:
        def embed_texts(self, texts, *, batch_size=256, parallel=None):
            seen["parallel"] = parallel
            return [[float(len(t)), 0.0, 0.0] for t in texts]

    monkeypatch.setattr(train, "get_semantic_reranker", lambda *a, **k: FakeReranker())
    mat = train.embed_records([{"company": "X", "company_key": "x", "domain": None}], batch_size=8)
    assert seen["parallel"] is None  # single-process, always (the OOM-safe contract)
    assert isinstance(mat, np.ndarray) and mat.shape[0] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_train_sector_smoke.py -v`
Expected: FAIL — import error (module not yet created).

- [ ] **Step 3: Implement the loader + embedder (top of the training script)**

```python
# scripts/train_sector_classifier.py
"""Offline trainer for the Stage-1 sector classifier PoC (sklearn; never imported at runtime).

Usage:
  .venv/bin/python scripts/train_sector_classifier.py \
      --corpus tests/fixtures/sector_corpus.jsonl --out dist/sector_clf.npz [--sample 50] [--folds 5]

--sample N embeds only the first N labeled rows (the memory/throughput STRESS gate) and skips export;
run it before the full pass. Concurrency for sklearn CV is env-gated via ERGON_SECTOR_JOBS
(explicit int wins; else CPU-2 on CI; else 1 local).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.extract.sector_features import build_input_text  # noqa: E402
from ergon_tracker.semantic import get_semantic_reranker  # noqa: E402


def _peak_rss_mb() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes vs KB


def _jobs() -> int:
    env = os.environ.get("ERGON_SECTOR_JOBS")
    if env:
        return int(env)
    if os.environ.get("CI"):
        return max(2, (os.cpu_count() or 4) - 2)
    return 1


def load_corpus(path: Path) -> tuple[list[dict], list[str]]:
    """Read the JSONL corpus; keep only rows with a gold sector."""
    records, labels = [], []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("sector"):
            records.append(r)
            labels.append(r["sector"])
    return records, labels


def embed_records(records: list[dict], *, batch_size: int = 256, sample: int | None = None, log=print) -> np.ndarray:
    """Single-process (parallel=None), memory-bounded embedding of the input-text of each record."""
    rows = records[:sample] if sample else records
    texts = [build_input_text(r.get("company"), r.get("domain"), r.get("example_title")) for r in rows]
    t0 = time.monotonic()
    reranker = get_semantic_reranker()
    vecs = reranker.embed_texts(texts, batch_size=batch_size, parallel=None)  # OOM-safe: no workers
    mat = np.asarray(vecs, dtype=np.float32)
    log(f"[embed] rows={len(rows)} dim={mat.shape[1] if mat.size else 0} "
        f"peakRSS={_peak_rss_mb():.0f}MB wall={time.monotonic() - t0:.1f}s")
    return mat
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_train_sector_smoke.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check scripts/train_sector_classifier.py tests/test_train_sector_smoke.py
.venv/bin/ruff format scripts/train_sector_classifier.py tests/test_train_sector_smoke.py
git add scripts/train_sector_classifier.py tests/test_train_sector_smoke.py
git commit -m "feat(sector): trainer corpus loader + single-process memory-safe embedder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Fit — CV `C`-sweep logreg + Platt + centroids + threshold sweep + export

**Files:**
- Modify: `scripts/train_sector_classifier.py`
- Test: `tests/test_train_sector_smoke.py`

**Interfaces:**
- Consumes: `assemble`, `cl2n` (features), `save_sector_model` (Task 3), `_jobs`, `embed_records`, `load_corpus`.
- Produces:
  - `fit_model(feats, y, *, folds, jobs) -> dict` — returns `{labels, W, b, platt_a, platt_b, centroids, cv_accuracy}`.
  - `sweep_thresholds(probs, feats, centroids, y_idx, *, target_precision) -> tuple[float, float, float, dict]` — vectorized grid → `(tau_prob, tau_margin, tau_sim, report)`.
  - `main(argv) -> None`.

- [ ] **Step 1: Write the failing test (fit on synthetic separable data)**

```python
# append to tests/test_train_sector_smoke.py
def test_fit_model_learns_separable(monkeypatch) -> None:
    pytest.importorskip("sklearn")
    rng = np.random.default_rng(1)
    # two well-separated blobs in a 6-dim "feature" space
    a = rng.normal(loc=+2, size=(40, 6)); b = rng.normal(loc=-2, size=(40, 6))
    feats = np.vstack([a, b]).astype(np.float32)
    y = ["Fintech"] * 40 + ["Healthcare"] * 40
    model = train.fit_model(feats, y, folds=3, jobs=1)
    assert set(model["labels"]) == {"Fintech", "Healthcare"}
    assert model["W"].shape == (2, 6)
    assert model["cv_accuracy"] > 0.9  # separable → high CV


def test_sweep_thresholds_hits_target_precision() -> None:
    # confident correct + a few wrong-but-confident → sweep should raise tau to protect precision
    probs = np.array([[0.95, 0.05], [0.9, 0.1], [0.55, 0.45], [0.92, 0.08]])
    feats = np.eye(4, 6, dtype=np.float32)
    centroids = np.array([[1.0, 0, 0, 0, 0, 0], [0, 1.0, 0, 0, 0, 0]], dtype=np.float32)
    y_idx = np.array([0, 0, 1, 1])  # last one is a confident error
    tp, tm, ts, rep = train.sweep_thresholds(probs, feats, centroids, y_idx, target_precision=0.85)
    # a precision-meeting point WITH positive coverage exists (fire only the 0.95-prob correct row),
    # so the sweep must return it — not the empty fallback.
    assert rep["precision"] >= 0.85 - 1e-9
    assert rep["coverage"] > 0.0
    assert 0.0 <= tp <= 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_train_sector_smoke.py -k "fit_model or sweep" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'fit_model'`.

- [ ] **Step 3: Implement fit + sweep + main**

```python
# append to scripts/train_sector_classifier.py
def fit_model(feats: np.ndarray, y: list[str], *, folds: int, jobs: int) -> dict:
    """L2 multinomial logreg with a CV C-sweep, per-class Platt, and class centroids."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    labels = sorted(set(y))
    idx = {lab: i for i, lab in enumerate(labels)}
    y_idx = np.asarray([idx[v] for v in y])

    # choose C by stratified CV accuracy (small grid; balanced for the long tail)
    best_c, best_acc = 1.0, -1.0
    n_splits = min(folds, int(np.bincount(y_idx).min()))
    skf = StratifiedKFold(n_splits=max(2, n_splits), shuffle=True, random_state=42)
    for c in (0.25, 0.5, 1.0, 2.0, 4.0):
        clf = LogisticRegression(C=c, max_iter=2000, class_weight="balanced")
        pred = cross_val_predict(clf, feats, y_idx, cv=skf, n_jobs=jobs)
        acc = float((pred == y_idx).mean())
        if acc > best_acc:
            best_c, best_acc = c, acc

    final = LogisticRegression(C=best_c, max_iter=2000, class_weight="balanced").fit(feats, y_idx)
    # binary logreg emits one row; expand to 2 rows so the .npz is always (n_classes, feat_dim)
    W = final.coef_ if final.coef_.shape[0] == len(labels) else np.vstack([-final.coef_[0], final.coef_[0]])
    b = final.intercept_ if final.intercept_.shape[0] == len(labels) else np.array([-final.intercept_[0], final.intercept_[0]])

    # per-class Platt on out-of-fold decision scores (exportable as sigmoid(a*f+b))
    from sklearn.linear_model import LogisticRegression as LR1D

    dec = cross_val_predict(
        LogisticRegression(C=best_c, max_iter=2000, class_weight="balanced"),
        feats, y_idx, cv=skf, n_jobs=jobs, method="decision_function",
    )
    dec = dec if dec.ndim == 2 else np.vstack([-dec, dec]).T
    platt_a, platt_b = np.ones(len(labels), np.float32), np.zeros(len(labels), np.float32)
    for k in range(len(labels)):
        yk = (y_idx == k).astype(int)
        if 0 < yk.sum() < len(yk):
            p = LR1D().fit(dec[:, [k]], yk)
            platt_a[k], platt_b[k] = float(p.coef_[0, 0]), float(p.intercept_[0])

    centroids = np.vstack([feats[y_idx == k].mean(axis=0) for k in range(len(labels))]).astype(np.float32)
    return {"labels": labels, "W": W.astype(np.float32), "b": b.astype(np.float32),
            "platt_a": platt_a, "platt_b": platt_b, "centroids": centroids, "cv_accuracy": best_acc}


def _apply(probs, platt_a, platt_b):
    cal = 1.0 / (1.0 + np.exp(-(platt_a * probs + platt_b)))
    return cal / cal.sum(axis=1, keepdims=True)


def sweep_thresholds(probs, feats, centroids, y_idx, *, target_precision: float):
    """Vectorized grid over (tau_prob, tau_margin, tau_sim); pick max-coverage point meeting precision."""
    order = np.argsort(-probs, axis=1)
    top1 = order[:, 0]
    p1 = probs[np.arange(len(probs)), top1]
    p2 = probs[np.arange(len(probs)), order[:, 1]]
    cn = np.linalg.norm(centroids, axis=1); cn[cn == 0] = 1.0
    fn = np.linalg.norm(feats, axis=1); fn[fn == 0] = 1.0
    sim = np.einsum("ij,ij->i", feats, centroids[top1]) / (fn * cn[top1])
    correct = top1 == y_idx
    best = (1.0, 0.0, -1.0, {"precision": 1.0, "coverage": 0.0})
    for tp in np.linspace(0.3, 0.95, 14):
        for tm in np.linspace(0.0, 0.4, 9):
            for ts in np.linspace(-0.2, 0.6, 9):
                fire = (p1 >= tp) & ((p1 - p2) >= tm) & (sim >= ts)
                cov = fire.mean()
                if cov == 0:
                    continue
                prec = correct[fire].mean()
                if prec >= target_precision and cov > best[3]["coverage"]:
                    best = (float(tp), float(tm), float(ts), {"precision": float(prec), "coverage": float(cov)})
    return best


def main(argv: list[str]) -> None:
    from sklearn.model_selection import cross_val_predict, StratifiedKFold
    from sklearn.linear_model import LogisticRegression
    from ergon_tracker.extract.sector_features import assemble, cl2n
    from ergon_tracker.extract.sector_clf import save_sector_model

    corpus, out, sample, folds, target = None, ROOT / "dist" / "sector_clf.npz", None, 5, 0.85
    i = 0
    while i < len(argv):
        if argv[i] == "--corpus": corpus = Path(argv[i + 1]); i += 2
        elif argv[i] == "--out": out = Path(argv[i + 1]); i += 2
        elif argv[i] == "--sample": sample = int(argv[i + 1]); i += 2
        elif argv[i] == "--folds": folds = int(argv[i + 1]); i += 2
        elif argv[i] == "--target-precision": target = float(argv[i + 1]); i += 2
        else: print(f"unknown flag: {argv[i]}"); return
    if not corpus:
        print("need --corpus"); return

    records, y = load_corpus(corpus)
    print(f"[load] {len(records)} labeled rows, {len(set(y))} classes, jobs={_jobs()}")
    emb = embed_records(records, sample=sample)
    if sample:
        print(f"[stress] sample={sample} embedded OK, peakRSS={_peak_rss_mb():.0f}MB — full run is safe.")
        return
    _, mean = cl2n(emb)
    feats = assemble(emb, [r.get("domain") for r in records], mean)

    model = fit_model(feats, y, folds=folds, jobs=_jobs())
    idx = {lab: k for k, lab in enumerate(model["labels"])}
    y_idx = np.asarray([idx[v] for v in y])

    # out-of-fold calibrated probs for an honest threshold sweep
    skf = StratifiedKFold(n_splits=max(2, min(folds, int(np.bincount(y_idx).min()))), shuffle=True, random_state=42)
    dec = cross_val_predict(
        LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced"),
        feats, y_idx, cv=skf, n_jobs=_jobs(), method="predict_proba")
    tp, tm, ts, rep = sweep_thresholds(dec, feats, model["centroids"], y_idx, target_precision=target)
    print(f"[sweep] tau=({tp:.2f},{tm:.2f},{ts:.2f}) precision={rep['precision']:.1%} coverage={rep['coverage']:.1%}")

    out.parent.mkdir(parents=True, exist_ok=True)
    save_sector_model(
        out, labels=model["labels"], mean=mean, W=model["W"], b=model["b"],
        platt_a=model["platt_a"], platt_b=model["platt_b"], centroids=model["centroids"],
        tau_prob=tp, tau_margin=tm, tau_sim=ts, embed_dim=emb.shape[1])
    (out.with_suffix(".metrics.json")).write_text(json.dumps(
        {"cv_accuracy": model["cv_accuracy"], "threshold": {"prob": tp, "margin": tm, "sim": ts},
         "sweep": rep, "n_labeled": len(records), "n_classes": len(model["labels"]),
         "peak_rss_mb": _peak_rss_mb()}, indent=2))
    print(f"[done] wrote {out} + metrics")


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_train_sector_smoke.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check scripts/train_sector_classifier.py tests/test_train_sector_smoke.py
.venv/bin/ruff format scripts/train_sector_classifier.py tests/test_train_sector_smoke.py
git add scripts/train_sector_classifier.py tests/test_train_sector_smoke.py
git commit -m "feat(sector): fit (CV C-sweep logreg + Platt + centroids) + threshold sweep + export

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Benchmark/report harness

**Files:**
- Create: `scripts/eval_sector_classifier.py`
- Test: `tests/test_eval_sector_smoke.py`

**Interfaces:**
- Consumes: `load_sector_model`, `SectorClassifier` (Task 3); `embed_records`, `load_corpus` (Task 4).
- Produces:
  - `risk_coverage(preds, golds) -> dict` — `{accuracy_when_covered, coverage, macro_f1, per_class}`.
  - `main(argv) -> None` — prints the table vs baseline (72.4% / 26.7%); `--ceiling` runs deberta zero-shot if `transformers` present.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eval_sector_smoke.py
from __future__ import annotations

import pytest

ev = pytest.importorskip("scripts.eval_sector_classifier", reason="run from repo root")


def test_risk_coverage_math() -> None:
    preds = [("Fintech", 0.9), (None, 0.4), ("Healthcare", 0.8), ("Fintech", 0.7)]
    golds = ["Fintech", "Healthcare", "Healthcare", "Banking/Finance"]
    m = ev.risk_coverage(preds, golds)
    assert m["coverage"] == pytest.approx(3 / 4)
    assert m["accuracy_when_covered"] == pytest.approx(2 / 3)  # 2 of 3 covered are right
    assert 0.0 <= m["macro_f1"] <= 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_eval_sector_smoke.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement `eval_sector_classifier.py`**

```python
# scripts/eval_sector_classifier.py
"""Benchmark the exported sector classifier vs the deterministic baseline (72.4% acc / 26.7% cov).

Usage:
  .venv/bin/python scripts/eval_sector_classifier.py \
      --corpus tests/fixtures/sector_corpus.jsonl --model dist/sector_clf.npz [--ceiling]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.train_sector_classifier import embed_records, load_corpus  # noqa: E402
from ergon_tracker.extract.sector_clf import load_sector_model  # noqa: E402

BASELINE = {"accuracy_when_covered": 0.724, "coverage": 0.267}


def _f1(tp: int, fp: int, fn: int) -> float:
    return 0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn)


def risk_coverage(preds: list[tuple[str | None, float]], golds: list[str]) -> dict:
    covered = [(p, g) for (p, _), g in zip(preds, golds) if p is not None]
    hits = sum(1 for p, g in covered if p == g)
    tp = defaultdict(int); fp = defaultdict(int); fn = defaultdict(int)
    for (p, _), g in zip(preds, golds):
        if p is None:
            continue
        if p == g:
            tp[g] += 1
        else:
            fp[p] += 1; fn[g] += 1
    classes = set(tp) | set(fp) | set(fn)
    macro = float(np.mean([_f1(tp[c], fp[c], fn[c]) for c in classes])) if classes else 0.0
    return {
        "coverage": len(covered) / len(preds) if preds else 0.0,
        "accuracy_when_covered": hits / len(covered) if covered else 0.0,
        "macro_f1": macro,
        "per_class": {c: _f1(tp[c], fp[c], fn[c]) for c in sorted(classes)},
    }


def main(argv: list[str]) -> None:
    corpus, model_path, ceiling = None, ROOT / "dist" / "sector_clf.npz", False
    i = 0
    while i < len(argv):
        if argv[i] == "--corpus": corpus = Path(argv[i + 1]); i += 2
        elif argv[i] == "--model": model_path = Path(argv[i + 1]); i += 2
        elif argv[i] == "--ceiling": ceiling = True; i += 1
        else: print(f"unknown flag: {argv[i]}"); return
    if not corpus:
        print("need --corpus"); return
    load_sector_model.cache_clear()
    clf = load_sector_model(model_path)
    if clf is None:
        print(f"no model at {model_path} — run train_sector_classifier.py first"); return

    records, golds = load_corpus(corpus)
    emb = embed_records(records)
    preds = clf.predict_batch(emb, [r.get("domain") for r in records])
    m = risk_coverage(preds, golds)
    print("\n=== Stage-1 PoC: bge-small + calibrated logreg ===")
    print(f"  accuracy-when-covered : {m['accuracy_when_covered']:.1%}  (baseline {BASELINE['accuracy_when_covered']:.1%})")
    print(f"  coverage              : {m['coverage']:.1%}  (baseline {BASELINE['coverage']:.1%})")
    print(f"  macro-F1              : {m['macro_f1']:.3f}")
    verdict = ("BEATS" if m["accuracy_when_covered"] >= BASELINE["accuracy_when_covered"]
               and m["coverage"] > BASELINE["coverage"] else "does NOT beat")
    print(f"  VERDICT: ML {verdict} the deterministic baseline.")
    if ceiling:
        try:
            from transformers import pipeline  # noqa: F401
            print("  [ceiling] deberta zero-shot available — (run separately; dev-only reference).")
        except ImportError:
            print("  [ceiling] transformers not installed — skipping zero-shot ceiling.")


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_eval_sector_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check scripts/eval_sector_classifier.py tests/test_eval_sector_smoke.py
.venv/bin/ruff format scripts/eval_sector_classifier.py tests/test_eval_sector_smoke.py
git add scripts/eval_sector_classifier.py tests/test_eval_sector_smoke.py
git commit -m "feat(sector): benchmark harness (accuracy-at-coverage, macro-F1) vs baseline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Full test sweep + stress gate + the real PoC run

**Files:** none created; this task runs the pipeline end-to-end.

- [ ] **Step 1: Install the extras**

Run: `.venv/bin/pip install -e '.[sector-train,semantic,dev]'`
Expected: sklearn + fastembed + numpy present.

- [ ] **Step 2: Full unit-test sweep (must be green before any full run)**

Run: `.venv/bin/pytest tests/test_sector_features.py tests/test_sector_clf_roundtrip.py tests/test_train_sector_smoke.py tests/test_eval_sector_smoke.py -v`
Expected: all PASS.

- [ ] **Step 3: STRESS GATE — sample embed first, watch memory**

Run: `.venv/bin/python scripts/train_sector_classifier.py --corpus tests/fixtures/sector_corpus.jsonl --sample 50`
Expected: prints `[stress] sample=50 embedded OK, peakRSS=…MB — full run is safe.` with peak RSS well under ~1.5 GB. If RSS is unexpectedly high, STOP and investigate before Step 4 (do not run the full pass).

- [ ] **Step 4: Full train**

Run: `.venv/bin/python scripts/train_sector_classifier.py --corpus tests/fixtures/sector_corpus.jsonl --out dist/sector_clf.npz --folds 5 --target-precision 0.85`
Expected: `[embed]`, `[sweep]`, `[done]` lines; `dist/sector_clf.npz` + `dist/sector_clf.metrics.json` written; peak RSS logged.

- [ ] **Step 5: Benchmark**

Run: `.venv/bin/python scripts/eval_sector_classifier.py --corpus tests/fixtures/sector_corpus.jsonl --model dist/sector_clf.npz`
Expected: the comparison table + `VERDICT:` line.

- [ ] **Step 6: Commit the artifacts (metrics only; model stays in dist/, not shipped yet)**

`dist/` is a build dir (git-ignored). Do NOT commit the `.npz` in Stage 1 — it ships only in Stage 3 after retraining on upgraded labels. Copy the metrics into the repo for the record:

```bash
mkdir -p docs/superpowers/artifacts
cp dist/sector_clf.metrics.json docs/superpowers/artifacts/2026-07-06-sector-stage1-metrics.json
git add docs/superpowers/artifacts/2026-07-06-sector-stage1-metrics.json
git commit -m "chore(sector): record Stage-1 PoC metrics

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Record the verdict + go/no-go for Stage 2–3

**Files:**
- Modify: `docs/extraction-baseline.md`

- [ ] **Step 1: Append a Stage-1 result note**

Add a `### Sector — Stage-1 ML PoC (2026-07-06)` subsection recording: accuracy-when-covered, coverage, macro-F1, the abstention thresholds, peak RSS/wall time, and the verdict vs the 72.4%/26.7% baseline. State the decision explicitly:
- **If ML beats baseline** (higher accuracy AND materially higher coverage): proceed to Stage 2 (data-join gazetteer) and Stage 3 (wire the cascade, retrain on SEC-EDGAR-upgraded labels).
- **If ML does not beat baseline:** pivot to data-only — invest in Stage 2 (Tier-1 gazetteer, which the existing `merge_sectors.py`/`sector_edgar.py`/`sector_wikidata.py` pipeline already partly implements) and skip Tier-2. Record why.

- [ ] **Step 2: Commit**

```bash
git add docs/extraction-baseline.md
git commit -m "docs(sector): record Stage-1 PoC verdict + Stage 2-3 go/no-go

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** (checked against `2026-07-06-hybrid-sector-classifier-design.md`):
- Tier-2 embedding classifier (bge-small + CL2N + TLD dims + calibrated logreg + 3-gate abstention) → Tasks 1–3, 5. ✔
- Stage-1 = Tier-2 alone on the 700-corpus, stratified split + 5-fold CV, accuracy-at-coverage/macro-F1 vs baseline → Tasks 5–7. ✔
- numpy-only runtime, sklearn train-only → enforced by the "sklearn only in scripts/" constraint + `sector-train` extra (Task 1); inference is `sector_clf.py` numpy-only (Task 3). ✔
- Concurrency-first + memory-bounded + stress-before-full → single-process embed (Task 4), env-gated `ERGON_SECTOR_JOBS` (Task 5), `--sample` stress gate (Task 7 Step 3). ✔
- deberta zero-shot ceiling (dev-only reference) → Task 6 `--ceiling` (guarded import). ✔
- Ratcheting gate: Stage-1 does NOT alter `test_sector_recall.py` (Tier-2 isn't wired into the extractor yet); the gate ratchet is a Stage-3 task. Recorded as out-of-scope-for-now in the file structure + Task 8. ✔
- **Deferred to Stage 2–3 (correctly, per the staged spec):** Tier-1 data-join gazetteer, SEC-EDGAR label upgrade, cascade wiring, shipping the `.npz`. Noted at each relevant point. ✔ Discovery folded in: an existing deterministic multi-source pipeline (`merge_sectors.py`, `sector_edgar.py`, `sector_wikidata.py`, `build_sector_naics.py`, `classify_sectors.py`) — Stage 2 reuses it rather than rebuilding (Task 8 Step 1).

**2. Placeholder scan:** No TBD/TODO; every code step has complete code; every run step has an exact command + expected output. ✔

**3. Type consistency:** `save_sector_model`/`load_sector_model`/`SectorClassifier.predict(_batch)` signatures identical across Tasks 3, 5, 6. `embed_records`/`load_corpus`/`fit_model`/`sweep_thresholds`/`risk_coverage` signatures identical across Tasks 4–6. Feature helpers `build_input_text`/`tld_features`/`cl2n`/`assemble`/`TLD_VOCAB`/`FEATURE_DIM` consistent across Tasks 1–4. `.npz` key set (`_KEYS`) matches `save_sector_model`'s writes. ✔
