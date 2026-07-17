"""Run-assembly glue: wire ``predict.py`` -> ``triage.py`` -> ``resolve_gold.py`` into the three
composable stages of a Filter Benchmark v2 run (Task 6/11-class glue, "assemble").

    python -m scripts.bench.assemble --stage predict  --corpus bench/corpus_jd.jsonl \\
        --out bench/predictions.jsonl
    python -m scripts.bench.assemble --stage queue    --corpus bench/corpus_jd.jsonl \\
        --predictions bench/predictions.jsonl --labels bench/labels.jsonl \\
        --out bench/audit_queue.jsonl
    python -m scripts.bench.assemble --stage resolve  --corpus bench/corpus_jd.jsonl \\
        --predictions bench/predictions.jsonl --labels bench/labels.jsonl \\
        --corrections bench/corrections.jsonl --out bench/resolved.jsonl

Every stage is a pure function (``predict_corpus``, ``build_audit_queue``, ``assemble_resolved``)
plus a thin CLI wrapper. All I/O is JSONL via ``schema.read_jsonl``/``write_jsonl``, which already
treats a missing file as an empty list -- so ``--labels``/``--corrections`` can point at files that
don't exist yet (labels/corrections come from later stages of the human pipeline) without any
special-casing here.

Corpus rows (``schema.corpus_row``) and extractor predictions (``predict.predict()``) are joined
by ``id`` -- NOT by list position -- because ``predictions.jsonl``, ``labels.jsonl``, and
``corrections.jsonl`` are separate files written at different times by different stages and are
not guaranteed to share row order or even row set (e.g. a labeling batch may cover only a subset
of the corpus). The two modules this glue calls into, ``triage.build_queue`` and
``resolve_gold.resolve``, both require their ``rows``/``preds``/``labels`` arguments to be
POSITIONALLY matched (parallel, same-length lists, joined via ``zip(..., strict=True)``) -- so
each stage here re-derives those parallel lists from the id-keyed join, driven by whichever input
carries the row set that matters for that stage (``labels`` for both "queue" and "resolve": a row
with no label yet has nothing to triage or resolve).

``predictions.jsonl`` rows: ``{"id": <str>, "source": <str>, "pred": {field: value, ...}}`` --
``pred`` is exactly ``predict.predict()``'s output shape (one entry per
``scripts.bench.schema.FIELDS``).

``resolved.jsonl`` rows: ``{"id": <str>, "source": <str>, "fields": {field: {"value",
"review_state", "extractor_value", "fleet_value"}}}`` -- exactly ``resolve_gold.resolve()``'s
per-row output PLUS a row-level ``source`` folded in from the matching corpus row, which is what
``scripts.bench.score._rows_for_field`` (and therefore ``scoring.score_field``/
``provider_matrix``) is documented to expect: "whatever assembles resolved.jsonl in practice is
expected to fold the corpus row's source back onto each row so the provider matrix can group by
ATS". Per field, that consumer reads ``entry["value"]`` as gold and ``entry["extractor_value"]``
as pred -- both already present on every ``resolve_gold.resolve()`` field entry, so this module
adds nothing there, only the row-level ``source``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import resolve_gold, triage
from .predict import predict
from .schema import read_jsonl, write_jsonl

__all__ = [
    "predict_corpus",
    "build_audit_queue",
    "assemble_resolved",
    "main",
]


def _derive_source(row: dict[str, Any]) -> str:
    """A corpus row's ``source`` field, falling back to the ``id`` prefix before ``":"`` (the
    ``"<source>:<source_job_id>"`` convention ``crawl_corpus.row_from_job`` writes), and finally
    ``"unknown"`` -- mirrors ``score.py``'s ``_rows_for_field`` fallback so a row missing
    ``source`` is grouped the same way whether it arrives via this glue or via a hand-built
    ``resolved.jsonl``."""
    source = row.get("source")
    if source:
        return str(source)
    row_id = row.get("id")
    if row_id and ":" in str(row_id):
        return str(row_id).split(":", 1)[0]
    return "unknown"


def predict_corpus(corpus_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run ``predict.predict()`` over every ``corpus_rows`` row: one predrow per corpus row,
    ``{"id", "source", "pred"}`` (see module docstring for the exact shape)."""
    return [
        {
            "id": row.get("id", ""),
            "source": _derive_source(row),
            "pred": predict(row),
        }
        for row in corpus_rows
    ]


def build_audit_queue(
    corpus_rows: list[dict[str, Any]],
    predrows: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    *,
    calib: int = 100,
) -> list[dict[str, Any]]:
    """The triage-ordered human audit queue (``triage.build_queue``), enriched with the posting
    context the Label Auditor UI (``label_auditor.html``) displays per item: ``title``, ``source``,
    ``company``, ``location_raw``, ``description_text``, ``url`` -- all pulled from the matching
    corpus row by ``id``.

    ``labels`` drives which rows are in scope: only rows with BOTH a label and a prediction are
    triaged (a row not yet labeled has nothing to triage against). Rows referenced by ``labels``
    but missing from ``corpus_rows``/``predrows`` are silently skipped rather than raising --
    this stage runs against whatever slice of the corpus has been labeled so far, and a stale/
    out-of-sync label file should degrade gracefully, not crash the CLI.
    """
    corpus_by_id = {row.get("id"): row for row in corpus_rows}
    pred_by_id = {p["id"]: p.get("pred", {}) for p in predrows}

    rows: list[dict[str, Any]] = []
    preds: list[dict[str, Any]] = []
    matched_labels: list[dict[str, Any]] = []
    for label in labels:
        rid = label.get("id")
        corpus_row_ = corpus_by_id.get(rid)
        pred = pred_by_id.get(rid)
        if corpus_row_ is None or pred is None:
            continue
        rows.append(corpus_row_)
        preds.append(pred)
        matched_labels.append(label)

    queue = triage.build_queue(rows, preds, matched_labels, calib=calib)

    for item in queue:
        context = corpus_by_id.get(item["id"], {})
        item["title"] = context.get("title", "")
        item["source"] = context.get("source", "") or _derive_source(context)
        item["company"] = context.get("company", "")
        item["location_raw"] = context.get("location_raw", "")
        item["description_text"] = context.get("description_text", "")
        if not item.get("url"):
            item["url"] = context.get("apply_url", "")

    return queue


def assemble_resolved(
    corpus_rows: list[dict[str, Any]],
    predrows: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fuse labels/predictions/corrections (``resolve_gold.resolve``) into resolved rows, then
    fold each row's corpus ``source`` on -- the exact ``resolved.jsonl`` shape ``score.py``/
    ``scoring.score_field`` consume (see module docstring).

    Like ``build_audit_queue``, ``labels`` drives row scope: a row is only resolved if it also has
    a matching prediction (matched by ``id``); a label with no prediction is skipped rather than
    raising, since ``resolve_gold.resolve`` itself requires positionally-parallel ``labels``/
    ``preds`` lists (``zip(..., strict=True)``).
    """
    corpus_by_id = {row.get("id"): row for row in corpus_rows}
    pred_by_id = {p["id"]: p.get("pred", {}) for p in predrows}

    matched_labels: list[dict[str, Any]] = []
    preds: list[dict[str, Any]] = []
    for label in labels:
        rid = label.get("id")
        pred = pred_by_id.get(rid)
        if pred is None:
            continue
        matched_labels.append(label)
        preds.append(pred)

    resolved = resolve_gold.resolve(matched_labels, preds, corrections)

    for row in resolved:
        corpus_row_ = corpus_by_id.get(row["id"], {})
        row["source"] = _derive_source(corpus_row_ or {"id": row["id"]})

    return resolved


def _write_calibration(
    path: Path, resolved: list[dict[str, Any]], corrections: list[dict[str, Any]]
) -> None:
    stats = resolve_gold.calibration_stats(resolved, corrections)
    path.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Filter Benchmark v2 run-assembly glue: predict / queue / resolve stages."
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=["predict", "queue", "resolve"],
        help="Which stage to run.",
    )
    parser.add_argument("--corpus", required=True, help="Path to corpus_jd.jsonl.")
    parser.add_argument(
        "--predictions",
        default=None,
        help="Path to predictions.jsonl (required for --stage queue/resolve).",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help="Path to labels.jsonl (Task-5 fleet labels). Omit if none exist yet.",
    )
    parser.add_argument(
        "--corrections",
        default=None,
        help="Path to corrections.jsonl (Task-7 human verdicts, --stage resolve only). Omit if "
        "none exist yet.",
    )
    parser.add_argument(
        "--calib",
        type=int,
        default=100,
        help="Calibration-sample size for --stage queue (see triage.build_queue).",
    )
    parser.add_argument("--out", required=True, help="Output JSONL path.")
    args = parser.parse_args(argv)

    corpus_rows = read_jsonl(args.corpus)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.stage == "predict":
        predrows = predict_corpus(corpus_rows)
        write_jsonl(out_path, predrows)
        print(
            f"[assemble] predict: {len(corpus_rows)} corpus rows -> {len(predrows)} predictions -> {args.out}"
        )
        return

    if not args.predictions:
        parser.error(f"--stage {args.stage} requires --predictions")
    predrows = read_jsonl(args.predictions)
    labels = read_jsonl(args.labels) if args.labels else []

    if args.stage == "queue":
        queue = build_audit_queue(corpus_rows, predrows, labels, calib=args.calib)
        write_jsonl(out_path, queue)
        print(f"[assemble] queue: {len(labels)} labels -> {len(queue)} audit items -> {args.out}")
        return

    # --stage resolve
    corrections = read_jsonl(args.corrections) if args.corrections else []
    resolved = assemble_resolved(corpus_rows, predrows, labels, corrections)
    write_jsonl(out_path, resolved)
    calibration_path = out_path.with_name("calibration.json")
    _write_calibration(calibration_path, resolved, corrections)
    print(
        f"[assemble] resolve: {len(labels)} labels + {len(corrections)} corrections -> "
        f"{len(resolved)} resolved rows -> {args.out} (+ {calibration_path})"
    )


if __name__ == "__main__":
    main()
