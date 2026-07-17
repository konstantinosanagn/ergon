"""CLI: turn resolved bench rows into ``bench/report.md`` + ``bench/report.json`` (Task 10).

    python -m scripts.bench.score --resolved bench/resolved.jsonl --out bench/report

Reads ``resolved.jsonl`` (Task-8 ``resolve_gold.resolve()`` output: one row per posting,
``{"id", "fields": {field: {"value", "review_state", "extractor_value", "fleet_value"}}}`` --
``resolve()`` itself carries no provider; whatever assembles ``resolved.jsonl`` in practice is
expected to fold the corpus row's ``source`` back onto each row so the provider matrix can group
by ATS. A row missing ``source`` is grouped under ``"unknown"`` rather than dropped.

Per field, turns each resolved row into the ``{"source", "gold", "pred"}`` shape
``scripts.bench.scoring`` consumes: the resolved (adjudicated) ``value`` is gold, the preserved
``extractor_value`` is pred (see ``scoring`` module docstring for why this is the correct
resolve()->scoring handoff). Runs ``scoring.score_field``/``provider_matrix`` per field, folds in
``resolve_gold.calibration_stats`` from an optional sibling ``corrections.jsonl``, and writes both
a machine ``report.json`` (Task 12 reads this to ratchet the CI gates) and a human ``report.md``
(via ``scripts.bench.report.render_markdown``).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .report import render_markdown
from .resolve_gold import calibration_stats
from .schema import FIELDS, read_jsonl
from .scoring import provider_matrix, score_field

__all__ = ["build_report", "main"]


def _rows_for_field(resolved: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """``resolved`` rows -> the ``{"source", "gold", "pred"}`` rows ``scoring.score_field``/
    ``provider_matrix`` consume, for one ``field`` at a time (see module docstring)."""
    rows: list[dict[str, Any]] = []
    for row in resolved:
        entry = row.get("fields", {}).get(field)
        if entry is None:
            continue
        source = row.get("source")
        if not source:
            row_id = row.get("id")
            source = str(row_id).split(":", 1)[0] if row_id and ":" in str(row_id) else "unknown"
        rows.append(
            {
                "source": source,
                "gold": {field: entry.get("value")},
                "pred": {field: entry.get("extractor_value")},
            }
        )
    return rows


def build_report(
    resolved: list[dict[str, Any]], corrections: list[dict[str, Any]]
) -> dict[str, Any]:
    """Pure: ``resolved`` rows + ``corrections`` -> the machine report object that both
    ``render_markdown`` and ``report.json`` consume."""
    fields: dict[str, Any] = {}
    matrix: dict[str, Any] = {}
    for field in FIELDS:
        field_rows = _rows_for_field(resolved, field)
        fields[field] = score_field(field, field_rows)
        matrix[field] = provider_matrix(field, field_rows)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(resolved),
        "fields": fields,
        "matrix": matrix,
        "calibration": calibration_stats(resolved, corrections),
    }


def _write_report(out: str, report_obj: dict[str, Any]) -> None:
    md_path = Path(f"{out}.md")
    json_path = Path(f"{out}.json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report_obj), encoding="utf-8")
    json_path.write_text(json.dumps(report_obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Score resolved bench rows into a report.")
    parser.add_argument(
        "--resolved", required=True, help="Path to resolved.jsonl (Task-8 resolve() output)."
    )
    parser.add_argument(
        "--corrections",
        default=None,
        help="Path to corrections.jsonl (Task-7 human verdicts). Defaults to a "
        "'corrections.jsonl' sibling of --resolved; missing is treated as no corrections yet.",
    )
    parser.add_argument(
        "--out", required=True, help="Output path prefix; writes <out>.md and <out>.json."
    )
    args = parser.parse_args(argv)

    resolved_path = Path(args.resolved)
    resolved = read_jsonl(resolved_path)

    corrections_path = (
        Path(args.corrections)
        if args.corrections is not None
        else resolved_path.with_name("corrections.jsonl")
    )
    corrections = read_jsonl(corrections_path)

    report_obj = build_report(resolved, corrections)
    _write_report(args.out, report_obj)
    print(
        f"wrote {args.out}.md + {args.out}.json ({len(resolved)} rows, {len(corrections)} corrections)"
    )


if __name__ == "__main__":
    main()
