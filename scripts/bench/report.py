"""Pure Markdown rendering for the filter-benchmark report (bench v2, Task 10).

Consumes the machine "report object" assembled by ``scripts.bench.score`` (mirrors
``bench/report.json``):

    {
      "generated_at": <iso str>,
      "n_rows": <int>,
      "fields": {field: <score_field() output>, ...},          # one entry per schema.FIELDS
      "matrix": {field: {source: <score_field() output>, ...}}, # provider_matrix() per field
      "calibration": <calibration_stats() output> | None,
    }

where a "``score_field()`` output" is ``{n, accuracy, precision, recall, coverage, ci: (lo, hi)}``
(``scripts.bench.scoring.score_field``).

Presentation rule (Task-9 downstream note, load-bearing): ``accuracy`` and ``recall`` are
numerically identical by construction, so the per-field table shows the HONEST triad --
**coverage, precision, recall** -- and omits a redundant "accuracy" column that would just repeat
recall. Coverage is always shown SEPARATELY from precision: low coverage means "the ATS didn't
state it," never "the extractor missed it."

Every function here is pure (no file IO, no network) so it is fully unit-testable on synthetic
report objects.
"""

from __future__ import annotations

from typing import Any

from .schema import FIELDS

__all__ = ["render_markdown"]

# A provider x field cell needs at least this many rows before its precision is trustworthy
# enough to call out in the "worst cells" section -- otherwise a single wrong prediction on a
# 1-row slice would dominate the list.
DEFAULT_MIN_CELL_N = 20

# How many of the lowest-precision qualifying cells to show.
_WORST_CELLS_LIMIT = 15

_EMPTY_METRICS: dict[str, Any] = {
    "n": 0,
    "accuracy": 0.0,
    "precision": 0.0,
    "recall": 0.0,
    "coverage": 0.0,
    "ci": (0.0, 0.0),
}


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_ci(ci: Any) -> str:
    if not ci:
        return "-"
    lo, hi = ci[0], ci[1]
    return f"[{_fmt_pct(lo)}, {_fmt_pct(hi)}]"


def _render_field_table(fields: dict[str, Any]) -> list[str]:
    out = [
        "## Per-Field Metrics",
        "",
        "Coverage is data availability (did the ATS state it); precision/recall are extractor "
        "quality on top of that -- a field can have low coverage and still show perfect "
        "precision/recall on the rows it does have gold for.",
        "",
        "| Field | N | Coverage | Precision | Recall | 95% CI (recall) |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for field in FIELDS:
        m = fields.get(field, _EMPTY_METRICS)
        out.append(
            f"| {field} | {m.get('n', 0)} | {_fmt_pct(m.get('coverage', 0.0))} "
            f"| {_fmt_pct(m.get('precision', 0.0))} | {_fmt_pct(m.get('recall', 0.0))} "
            f"| {_fmt_ci(m.get('ci'))} |"
        )
    return out


def _render_matrix(matrix: dict[str, dict[str, Any]]) -> list[str]:
    out = [
        "## Provider x Field Matrix",
        "",
        "The headline per-ATS diagnostic: which providers "
        "are dragging a field down, field by field.",
    ]
    if not matrix:
        out.append("")
        out.append("_No per-provider data available._")
        return out
    for field in FIELDS:
        by_source = matrix.get(field)
        out.append("")
        out.append(f"### {field}")
        out.append("")
        if not by_source:
            out.append("_No per-provider data for this field._")
            continue
        out.append("| Provider | N | Coverage | Precision | Recall |")
        out.append("|---|---:|---:|---:|---:|")
        for source in sorted(by_source):
            m = by_source[source]
            out.append(
                f"| {source} | {m.get('n', 0)} | {_fmt_pct(m.get('coverage', 0.0))} "
                f"| {_fmt_pct(m.get('precision', 0.0))} | {_fmt_pct(m.get('recall', 0.0))} |"
            )
    return out


def _render_coverage_vs_precision(fields: dict[str, Any]) -> list[str]:
    out = [
        "## Coverage vs. Precision",
        "",
        "Sorted by coverage (ascending) so data-sparse fields are never mistaken for "
        "extractor misses -- a low-coverage / high-precision field means the ATS rarely states "
        "it, not that the extractor is bad at it.",
        "",
        "| Field | Coverage | Precision | Gap (precision - coverage) |",
        "|---|---:|---:|---:|",
    ]
    ordered = sorted(FIELDS, key=lambda f: fields.get(f, _EMPTY_METRICS).get("coverage", 0.0))
    for field in ordered:
        m = fields.get(field, _EMPTY_METRICS)
        coverage = m.get("coverage", 0.0)
        precision = m.get("precision", 0.0)
        out.append(
            f"| {field} | {_fmt_pct(coverage)} | {_fmt_pct(precision)} | {_fmt_pct(precision - coverage)} |"
        )
    return out


def _render_calibration(calibration: dict[str, Any] | None) -> list[str]:
    out = ["## Calibration", ""]
    if not calibration:
        out.append(
            "_No calibration data (no human corrections filed yet -- run the Label Auditor "
            "and ingest `corrections.jsonl` to populate this section)._"
        )
        return out
    out.extend(
        [
            f"- Human-audited corrections: **{calibration.get('n_corrections', 0)}**",
            f"- Human-verified fraction: **{_fmt_pct(calibration.get('human_verified_fraction', 0.0))}** "
            "(how often the human's check matched what auto-resolution had already picked)",
            f"- Auto-accepted agreements checked: **{calibration.get('n_agreements_checked', 0)}**",
            f"- False-agreement rate: **{_fmt_pct(calibration.get('false_agreement_rate', 0.0))}** "
            "(rate at which an extractor==fleet agreement was nonetheless wrong, measured on the "
            "audited slice)",
        ]
    )
    return out


def _render_worst_cells(matrix: dict[str, dict[str, Any]], min_cell_n: int) -> list[str]:
    out = [
        "## Worst Per-ATS Cells",
        "",
        f"The lowest-precision provider x field cells with at least {min_cell_n} rows -- the "
        "first places to look for extraction bugs.",
        "",
    ]
    cells: list[tuple[str, str, dict[str, Any]]] = []
    for field in FIELDS:
        for source, m in matrix.get(field, {}).items():
            if m.get("n", 0) >= min_cell_n:
                cells.append((field, source, m))

    if not cells:
        out.append(f"_No provider x field cell has at least {min_cell_n} rows yet._")
        return out

    cells.sort(key=lambda c: (c[2].get("precision", 0.0), -c[2].get("n", 0)))
    out.append("| Field | Provider | N | Precision | Coverage | Recall |")
    out.append("|---|---|---:|---:|---:|---:|")
    for field, source, m in cells[:_WORST_CELLS_LIMIT]:
        out.append(
            f"| {field} | {source} | {m.get('n', 0)} | {_fmt_pct(m.get('precision', 0.0))} "
            f"| {_fmt_pct(m.get('coverage', 0.0))} | {_fmt_pct(m.get('recall', 0.0))} |"
        )
    return out


def render_markdown(report_obj: dict[str, Any], *, min_cell_n: int = DEFAULT_MIN_CELL_N) -> str:
    """Render ``report_obj`` (see module docstring for the exact shape) to a Markdown report.

    Pure -- no file IO, no network. Sections, in order: per-field table (coverage/precision/
    recall + Wilson CI), the provider x field matrix (headline per-ATS diagnostic), a
    coverage-vs-precision view, calibration stats, and the worst per-ATS cells (lowest precision
    with enough sample size).
    """
    generated_at = report_obj.get("generated_at", "")
    n_rows = report_obj.get("n_rows", 0)
    fields = report_obj.get("fields", {})
    matrix = report_obj.get("matrix", {})
    calibration = report_obj.get("calibration")

    lines: list[str] = ["# Filter Benchmark v2 -- Report", ""]
    header = f"Rows: **{n_rows}**"
    if generated_at:
        header = f"Generated: {generated_at}  |  " + header
    lines.append(header)
    lines.append("")

    lines.extend(_render_field_table(fields))
    lines.append("")
    lines.extend(_render_matrix(matrix))
    lines.append("")
    lines.extend(_render_coverage_vs_precision(fields))
    lines.append("")
    lines.extend(_render_calibration(calibration))
    lines.append("")
    lines.extend(_render_worst_cells(matrix, min_cell_n))
    lines.append("")

    return "\n".join(lines)
