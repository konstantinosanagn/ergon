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


def embed_records(
    records: list[dict],
    *,
    batch_size: int = 256,
    sample: int | None = None,
    log=print,
) -> np.ndarray:
    """Single-process (parallel=None), memory-bounded embedding of the input-text of each record."""
    rows = records[:sample] if sample else records
    texts = [
        build_input_text(r.get("company"), r.get("domain"), r.get("example_title")) for r in rows
    ]
    t0 = time.monotonic()
    reranker = get_semantic_reranker()
    vecs = reranker.embed_texts(texts, batch_size=batch_size, parallel=None)  # OOM-safe: no workers
    mat = np.asarray(vecs, dtype=np.float32)
    log(
        f"[embed] rows={len(rows)} dim={mat.shape[1] if mat.size else 0} "
        f"peakRSS={_peak_rss_mb():.0f}MB wall={time.monotonic() - t0:.1f}s"
    )
    return mat
