"""Tests for scripts.bench.report (render_markdown) and the scripts.bench.score CLI end-to-end
smoke test.

``render_markdown`` is pure and unit-tested on a SYNTHETIC report object -- no network, no real
corpus. The CLI test crafts a tiny synthetic ``resolved.jsonl`` in a tmp dir and confirms
``report.md``/``report.json`` are written and parse.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from scripts.bench.report import render_markdown
from scripts.bench.schema import FIELDS
from scripts.bench.score import build_report, main


def _metrics(
    n: int = 10,
    coverage: float = 1.0,
    precision: float = 1.0,
    recall: float = 1.0,
    ci: tuple[float, float] = (0.9, 1.0),
) -> dict[str, Any]:
    return {
        "n": n,
        "accuracy": recall,
        "precision": precision,
        "recall": recall,
        "coverage": coverage,
        "ci": ci,
    }


def _synthetic_report(
    *, matrix: dict[str, dict[str, Any]] | None = None, calibration: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "generated_at": "2026-07-16T00:00:00+00:00",
        "n_rows": 42,
        "fields": {field: _metrics() for field in FIELDS},
        "matrix": matrix if matrix is not None else {},
        "calibration": calibration,
    }


# ---------------------------------------------------------------------------
# render_markdown(): shape assertions on a synthetic report object
# ---------------------------------------------------------------------------


def test_render_markdown_mentions_every_field():
    report = _synthetic_report()
    md = render_markdown(report)
    for field in FIELDS:
        assert field in md


def test_render_markdown_has_a_matrix_section():
    matrix = {
        "level": {
            "greenhouse": _metrics(n=50, coverage=0.9, precision=0.95, recall=0.9),
            "lever": _metrics(n=50, coverage=0.8, precision=0.6, recall=0.5),
        }
    }
    md = render_markdown(_synthetic_report(matrix=matrix))
    assert "Matrix" in md
    assert "greenhouse" in md
    assert "lever" in md


def test_render_markdown_shows_coverage_and_precision_as_distinct_columns():
    md = render_markdown(_synthetic_report())
    # The per-field table header carries both, and never a redundant third "Accuracy" column
    # that would just repeat recall (Task-9 downstream presentation note).
    header_line = next(line for line in md.splitlines() if line.startswith("| Field |"))
    assert "Coverage" in header_line
    assert "Precision" in header_line
    assert "Recall" in header_line
    assert "Accuracy" not in header_line


def test_render_markdown_high_coverage_low_precision_field_still_shows_both_numbers():
    # A field where the ATS states it often (high coverage) but the extractor is bad at it (low
    # precision) -- coverage and precision must never collapse into one number.
    fields = {field: _metrics() for field in FIELDS}
    fields["sector"] = _metrics(coverage=0.95, precision=0.2, recall=0.2)
    report = _synthetic_report()
    report["fields"] = fields
    md = render_markdown(report)
    sector_line = next(line for line in md.splitlines() if line.startswith("| sector |"))
    assert "95.0%" in sector_line
    assert "20.0%" in sector_line


def test_render_markdown_has_calibration_section_with_stats():
    calibration = {
        "n_corrections": 20,
        "n_confirmed": 15,
        "human_verified_fraction": 0.75,
        "n_agreements_checked": 12,
        "n_agreements_overturned": 2,
        "false_agreement_rate": 2 / 12,
    }
    md = render_markdown(_synthetic_report(calibration=calibration))
    assert "Calibration" in md
    assert "75.0%" in md  # human_verified_fraction


def test_render_markdown_calibration_none_does_not_crash_and_notes_absence():
    md = render_markdown(_synthetic_report(calibration=None))
    assert "Calibration" in md
    assert "No calibration data" in md


def test_render_markdown_worst_cells_section_present_and_sorted_by_precision():
    matrix = {
        "level": {
            "greenhouse": _metrics(n=100, precision=0.95),
            "workday": _metrics(n=100, precision=0.40),
        },
        "sector": {
            "icims": _metrics(n=100, precision=0.55),
        },
    }
    md = render_markdown(_synthetic_report(matrix=matrix), min_cell_n=1)
    assert "Worst" in md
    worst_section = md.split("## Worst Per-ATS Cells", 1)[1]
    # Lowest precision (workday, 40%) must appear before higher-precision cells in the section.
    assert worst_section.index("workday") < worst_section.index("icims")
    assert worst_section.index("icims") < worst_section.index("greenhouse")


def test_render_markdown_worst_cells_excludes_low_n_cells_below_threshold():
    matrix = {
        "level": {
            "tinyshop": _metrics(n=2, precision=0.0),  # worst precision, but too few rows
            "greenhouse": _metrics(n=100, precision=0.9),
        }
    }
    md = render_markdown(_synthetic_report(matrix=matrix), min_cell_n=20)
    worst_section = md.split("## Worst Per-ATS Cells", 1)[1]
    assert "tinyshop" not in worst_section
    assert "greenhouse" in worst_section


def test_render_markdown_worst_cells_empty_when_no_cell_meets_threshold():
    matrix = {"level": {"tinyshop": _metrics(n=2, precision=0.0)}}
    md = render_markdown(_synthetic_report(matrix=matrix), min_cell_n=20)
    worst_section = md.split("## Worst Per-ATS Cells", 1)[1]
    assert "No provider" in worst_section or "no provider" in worst_section.lower()


def test_render_markdown_empty_matrix_does_not_crash():
    md = render_markdown(_synthetic_report(matrix={}))
    assert "Matrix" in md
    assert "No per-provider data" in md


def test_render_markdown_is_deterministic():
    report = _synthetic_report(
        matrix={"level": {"greenhouse": _metrics(), "lever": _metrics(precision=0.5)}},
        calibration={
            "n_corrections": 1,
            "n_confirmed": 1,
            "human_verified_fraction": 1.0,
            "n_agreements_checked": 1,
            "n_agreements_overturned": 0,
            "false_agreement_rate": 0.0,
        },
    )
    assert render_markdown(report) == render_markdown(copy.deepcopy(report))


# ---------------------------------------------------------------------------
# score.build_report(): pure assembly from resolved rows + corrections
# ---------------------------------------------------------------------------

_BASELINE: dict[str, Any] = {
    "level": "senior",
    "sector": "Software/SaaS",
    "country": "United States",
    "city": "New York",
    "remote": False,
    "employment_type": "full_time",
    "salary": {"min": 150000, "max": 180000, "currency": "USD"},
    "yoe": {"min": 5, "max": 8},
    "degree": "bachelor",
    "sponsorship": True,
    "posted_at": "2024-01-01",
    "visa_sponsor": True,
}


def _resolved_row(rid: str, source: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """A resolve_gold.resolve()-shaped row, extended with a top-level "source" (as a real
    resolved.jsonl assembly step is expected to fold in), where extractor_value == fleet_value ==
    value for every field except any in ``overrides``."""
    fields: dict[str, Any] = {}
    for field in FIELDS:
        value = copy.deepcopy(_BASELINE[field])
        fields[field] = {
            "value": value,
            "review_state": "auto",
            "extractor_value": copy.deepcopy(value),
            "fleet_value": copy.deepcopy(value),
        }
    for field, entry_overrides in (overrides or {}).items():
        fields[field].update(entry_overrides)
    return {"id": rid, "source": source, "fields": fields}


def test_build_report_produces_every_field_and_matrix_entry():
    resolved = [
        _resolved_row("r1", "greenhouse"),
        _resolved_row("r2", "lever"),
    ]
    report = build_report(resolved, [])
    assert set(report["fields"]) == set(FIELDS)
    assert set(report["matrix"]) == set(FIELDS)
    assert set(report["matrix"]["level"]) == {"greenhouse", "lever"}
    assert report["n_rows"] == 2


def test_build_report_precision_reflects_extractor_disagreement():
    # Row where the resolved gold ("value") is "junior" but the extractor originally said
    # "senior" -- a real disagreement that survived to resolved gold (e.g. a human correction).
    resolved = [
        _resolved_row(
            "r1",
            "workday",
            overrides={
                "level": {"value": "junior", "extractor_value": "senior", "fleet_value": "junior"}
            },
        )
    ]
    report = build_report(resolved, [])
    assert report["fields"]["level"]["precision"] == 0.0
    assert report["fields"]["level"]["coverage"] == 1.0


def test_build_report_missing_source_groups_under_unknown():
    row = _resolved_row("r1", "")
    del row["source"]
    report = build_report([row], [])
    assert set(report["matrix"]["level"]) == {"unknown"}


def test_build_report_calibration_reflects_corrections():
    resolved = [_resolved_row("r1", "greenhouse")]
    corrections = [{"id": "r1", "field": "level", "verdict": "extractor"}]
    report = build_report(resolved, corrections)
    assert report["calibration"]["n_corrections"] == 1


# ---------------------------------------------------------------------------
# score.main(): end-to-end CLI smoke test on a tiny synthetic corpus
# ---------------------------------------------------------------------------


def test_score_cli_writes_report_md_and_json(tmp_path):
    rows = [
        _resolved_row("r1", "greenhouse"),
        _resolved_row("r2", "greenhouse"),
        _resolved_row("r3", "lever"),
        _resolved_row(
            "r4",
            "lever",
            overrides={
                "level": {"value": "junior", "extractor_value": "senior", "fleet_value": "junior"}
            },
        ),
        _resolved_row("r5", "workday"),
        _resolved_row("r6", "workday"),
    ]
    resolved_path = tmp_path / "resolved.jsonl"
    resolved_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    out_prefix = tmp_path / "report"
    main(["--resolved", str(resolved_path), "--out", str(out_prefix)])

    md_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    assert md_path.is_file()
    assert json_path.is_file()

    md = md_path.read_text(encoding="utf-8")
    for field in FIELDS:
        assert field in md

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["n_rows"] == 6
    assert set(payload["fields"]) == set(FIELDS)
    assert set(payload["matrix"]["level"]) == {"greenhouse", "lever", "workday"}
    assert payload["calibration"]["n_corrections"] == 0


def test_score_cli_reads_sibling_corrections_file_by_default(tmp_path):
    rows = [_resolved_row("r1", "greenhouse")]
    resolved_path = tmp_path / "resolved.jsonl"
    resolved_path.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")

    corrections_path = tmp_path / "corrections.jsonl"
    corrections_path.write_text(
        json.dumps({"id": "r1", "field": "level", "verdict": "extractor"}) + "\n",
        encoding="utf-8",
    )

    out_prefix = tmp_path / "report"
    main(["--resolved", str(resolved_path), "--out", str(out_prefix)])

    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["calibration"]["n_corrections"] == 1
